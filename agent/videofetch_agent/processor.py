"""Обработка одного блока идентификаторов пулом потоков с контрольными точками."""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable

from .client import CoordinatorClient, LeaseLost
from .fetcher import FetchSettings, Fetcher, Outcome, Stop
from .http import NetworkError
from .util import RateGate

ACTIVE_JOB_STATUSES = {"running", "pausing"}  # pausing: текущий блок дорабатываем


class Counters:
    """Счётчики для метрик: коды ответов, байты, число обработанных id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status: dict[str, int] = {}
        self.bytes = 0
        self.ids = 0
        self.active_checks = 0
        self.active_downloads = 0

    def add_status(self, code: int) -> None:
        with self._lock:
            self.status[str(code)] = self.status.get(str(code), 0) + 1

    def add_bytes(self, n: int) -> None:
        with self._lock:
            self.bytes += n

    def add_id(self) -> None:
        with self._lock:
            self.ids += 1

    def adjust(self, field: str, delta: int) -> None:
        with self._lock:
            setattr(self, field, getattr(self, field) + delta)

    def snapshot(self) -> dict:
        with self._lock:
            return {"status": dict(self.status), "bytes": self.bytes, "ids": self.ids,
                    "active_checks": self.active_checks, "active_downloads": self.active_downloads}


def settings_from(chunk_settings: dict, default_template: str) -> FetchSettings:
    return FetchSettings(
        url_template=chunk_settings.get("url_template") or default_template,
        storage_path=chunk_settings["storage_path"],
        request_timeout=chunk_settings.get("request_timeout_seconds", 10),
        max_retries=chunk_settings.get("max_retries", 3),
    )


class ChunkProcessor:
    def __init__(self, client: CoordinatorClient, gate: RateGate, counters: Counters, *, default_template: str = "",
                 report_every: int = 50, report_interval: float = 5.0, log: Callable[[str], None] = lambda m: None) -> None:
        self.client = client
        self.gate = gate
        self.counters = counters
        self.default_template = default_template
        self.report_every = report_every
        self.report_interval = report_interval
        self.log = log

    def process(self, chunk: dict, global_stop: threading.Event) -> str:
        """Обрабатывает блок. Возвращает: 'completed' | 'abandoned' (остановка/потеря аренды)."""
        stop = threading.Event()  # остановка только этого блока; глобальная останавливает и его
        cs = chunk["settings"]
        settings = settings_from(cs, self.default_template)
        self.gate.set_rate(cs.get("rate_limit_rps"))
        fetcher = Fetcher(self.gate, stop, counters=self.counters)

        start, end = chunk["next_id"], chunk["range_end"]
        pending_ids = iter(range(start, end + 1))
        in_flight: dict = {}
        done: dict[int, Outcome] = {}
        unsent: list[Outcome] = []
        checkpoint = start
        threads = max(1, cs.get("threads", 1))
        since_report = 0
        abort = False
        last_report = time.monotonic()

        def work(vid: int) -> Outcome:
            self.counters.adjust("active_checks", 1)
            try:
                outcome, meta = fetcher.check(vid, settings)
            finally:
                self.counters.adjust("active_checks", -1)
            if outcome.status == "found" and meta is not None:
                self.counters.adjust("active_downloads", 1)
                try:
                    outcome = fetcher.download(vid, meta, settings)
                finally:
                    self.counters.adjust("active_downloads", -1)
            return outcome

        def report(final: bool = False) -> bool:
            """Отправляет накопленное. Возвращает False, если блок надо бросить."""
            nonlocal unsent, since_report, threads, settings, last_report
            last_report = time.monotonic()
            items = [o.as_item() for o in unsent]
            try:
                if final:
                    for attempt in range(3):
                        try:
                            self.client.complete(chunk["chunk_id"], items)
                            return True
                        except NetworkError:
                            if attempt == 2:
                                self.log("не удалось отправить завершение блока; аренда истечёт и блок будет выдан повторно")
                                return False
                            time.sleep(1 + attempt)
                resp = self.client.progress(chunk["chunk_id"], checkpoint, items)
            except LeaseLost:
                self.log(f"аренда блока {chunk['chunk_id']} потеряна")
                return False
            except NetworkError as exc:
                self.log(f"координатор недоступен: {exc}")  # результаты остаются в unsent, повторим позже
                return True
            unsent, since_report = [], 0
            if resp["job_status"] not in ACTIVE_JOB_STATUSES:
                return False
            new = resp["settings"]  # настройки меняются «на лету»
            threads = max(1, new.get("threads", threads))
            self.gate.set_rate(new.get("rate_limit_rps"))
            settings = settings_from(new, settings.url_template)
            return True

        with ThreadPoolExecutor(max_workers=64, thread_name_prefix="fetch") as pool:
            try:
                exhausted = False
                while True:
                    while not exhausted and not abort and len(in_flight) < threads:
                        vid = next(pending_ids, None)
                        if vid is None:
                            exhausted = True
                            break
                        in_flight[pool.submit(work, vid)] = vid
                    if not in_flight:
                        break
                    finished, _ = wait(list(in_flight), timeout=1.0, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        vid = in_flight.pop(fut)
                        try:
                            outcome = fut.result()
                        except Stop:
                            abort = True
                            continue
                        done[vid] = outcome
                        unsent.append(outcome)
                        self.counters.add_id()
                        since_report += 1
                    while checkpoint in done:  # контрольная точка — первый незавершённый id
                        checkpoint += 1
                    if global_stop.is_set():
                        abort = True
                    due = since_report >= self.report_every or time.monotonic() - last_report >= self.report_interval
                    if not abort and due:
                        if not report():
                            abort = True
                    if abort:
                        stop.set()  # будим остальные потоки
            finally:
                stop.set()  # освобождает потоки, если вышли по исключению

        if abort or global_stop.is_set():
            return "abandoned"
        if len(done) != end - start + 1:
            raise RuntimeError("не все идентификаторы обработаны")
        return "completed" if report(final=True) else "abandoned"
