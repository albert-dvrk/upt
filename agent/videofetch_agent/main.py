"""Точка входа агента: регистрация, heartbeat, цикл «взять блок → обработать → отчитаться».

Запуск:  python -m videofetch_agent --coordinator http://host:8001 --name worker-01
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import threading
import time

from . import __version__
from .client import CoordinatorClient, CoordinatorError, LeaseLost
from .http import NetworkError
from .metrics import MetricsCollector
from .processor import ChunkProcessor, Counters
from .util import RateGate, backoff_delay

log = logging.getLogger("videofetch.agent")


class Agent:
    def __init__(self, client: CoordinatorClient, default_template: str, storage_hint: str = "/data/videos") -> None:
        self.client = client
        self.stop = threading.Event()
        self.counters = Counters()
        self.gate = RateGate()
        self.default_template = default_template
        self.storage_hint = storage_hint
        self.heartbeat_interval = 10.0
        self.processor = ChunkProcessor(client, self.gate, self.counters, default_template=default_template, log=log.warning)
        self.metrics: MetricsCollector | None = None

    # ---- запуск

    def register(self) -> None:
        attempt = 0
        while not self.stop.is_set():
            try:
                reg = self.client.register(__version__, None)
                self.heartbeat_interval = float(reg.get("heartbeat_interval_seconds", 10))
                self.storage_hint = reg.get("storage_path", self.storage_hint)
                self.metrics = MetricsCollector(self.storage_hint, self.counters)
                log.info("зарегистрирован как %s (id=%s)", self.client.name, self.client.worker_id)
                return
            except (NetworkError, CoordinatorError) as exc:
                attempt += 1
                delay = max(1.0, backoff_delay(attempt, 1.0, 30.0))
                log.warning("регистрация не удалась (%s), повтор через %.1f c", exc, delay)
                self.stop.wait(delay)

    def _heartbeat_loop(self) -> None:
        while not self.stop.wait(self.heartbeat_interval):
            try:
                info = self.metrics.collect() if self.metrics else None
                if info is not None:  # состояние выполнения: активные загрузки и масштаб адаптивного ограничения
                    info["runtime"] = {"downloads": self.counters.downloads_snapshot(), "throttle_scale": round(self.gate.scale, 2)}
                resp = self.client.heartbeat(info, self.metrics.resources() if self.metrics else None)
                for job in resp.get("jobs", []):  # лимит скорости применяется сразу, без новой аренды
                    self.gate.set_rate(job.get("rate_limit_rps"))
            except LeaseLost:
                log.error("сервер отключён координатором")
            except (NetworkError, CoordinatorError) as exc:
                log.warning("heartbeat не удался: %s", exc)

    def run(self) -> None:
        self.register()
        threading.Thread(target=self._heartbeat_loop, name="heartbeat", daemon=True).start()
        idle = 0
        while not self.stop.is_set():
            try:
                lease = self.client.lease()
            except LeaseLost:
                self.stop.wait(10)
                continue
            except (NetworkError, CoordinatorError) as exc:
                idle += 1
                delay = max(1.0, backoff_delay(idle, 1.0, 30.0))
                log.warning("не удалось получить блок: %s; повтор через %.1f c", exc, delay)
                self.stop.wait(delay)
                continue
            chunk = lease.get("chunk")
            if not chunk:
                idle = 0
                self.stop.wait(3.0)  # работы нет (reason=%s)
                continue
            log.info("блок %s: id %s..%s (попытка %s)", chunk["chunk_id"], chunk["range_start"], chunk["range_end"], chunk["attempt"])
            try:
                result = self.processor.process(chunk, self.stop)
                log.info("блок %s: %s", chunk["chunk_id"], result)
            except Exception as exc:  # noqa: BLE001 — любая ошибка блока не должна ронять агента
                log.exception("блок %s завершился ошибкой", chunk["chunk_id"])
                try:
                    self.client.fail(chunk["chunk_id"], "agent_error", repr(exc))
                except Exception:  # noqa: BLE001
                    pass
                self.stop.wait(1.0)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="videofetch_agent")
    p.add_argument("--coordinator", default=os.environ.get("VF_COORDINATOR_URL"), required=not os.environ.get("VF_COORDINATOR_URL"))
    p.add_argument("--name", default=os.environ.get("VF_WORKER_NAME"), required=not os.environ.get("VF_WORKER_NAME"))
    p.add_argument("--token", default=os.environ.get("VF_AGENT_TOKEN"))
    p.add_argument("--token-file", default=os.environ.get("VF_AGENT_TOKEN_FILE"), help="файл с токеном (предпочтительнее --token)")
    p.add_argument("--ca-file", default=os.environ.get("VF_CA_FILE"), help="сертификат координатора для HTTPS")
    p.add_argument("--url-template", default=os.environ.get("VF_DEFAULT_URL_TEMPLATE", ""))
    p.add_argument("--log-level", default=os.environ.get("VF_LOG_LEVEL", "INFO"))
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = args.token
    if args.token_file:
        with open(args.token_file, encoding="utf-8") as fh:
            token = fh.read().strip()
    agent = Agent(CoordinatorClient(args.coordinator, args.name, token=token, ca_file=args.ca_file), args.url_template)

    def shutdown(signum, _frame) -> None:
        log.info("получен сигнал %s, останавливаюсь", signum)
        agent.stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    agent.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
