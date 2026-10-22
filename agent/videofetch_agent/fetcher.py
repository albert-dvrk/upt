"""Проверка идентификатора и загрузка файла: повторы, докачка, проверка SHA-256."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from .http import NetworkError, open_stream, request
from .util import RateGate, backoff_delay, parse_retry_after, safe_join, video_filename


@dataclass
class FetchSettings:
    url_template: str
    storage_path: str
    request_timeout: float = 10
    max_retries: int = 3
    max_rate_limit_waits: int = 20
    min_free_bytes: int = 0


@dataclass
class Outcome:
    """Итог по одному идентификатору в терминах API координатора."""

    external_id: int
    status: str
    http_status: int | None = None
    error_code: str | None = None

    def as_item(self) -> dict:
        return {k: v for k, v in {"external_id": self.external_id, "status": self.status, "http_status": self.http_status, "error_code": self.error_code}.items() if v is not None}


class Stop(Exception):
    """Работа прервана (остановка задания или потеря аренды)."""


class Fetcher:
    def __init__(self, gate: RateGate, stop: threading.Event, *, sleep=None, counters=None) -> None:
        self.gate = gate
        self.stop = stop
        self._sleep = sleep or (lambda s: stop.wait(s))
        self.counters = counters  # объект с .add_status(code) / .add_bytes(n), необязательный

    # ------------------------------------------------------------ вспомогательное

    def _note_status(self, code: int) -> None:
        if self.counters:
            self.counters.add_status(code)

    def _pause(self, seconds: float) -> None:
        if self._sleep(seconds) or self.stop.is_set():
            if self.stop.is_set():
                raise Stop()

    def _get(self, url: str, s: FetchSettings, headers=None):
        self.gate.wait()
        return request("GET", url, headers=headers, timeout=s.request_timeout)

    # ------------------------------------------------------------ проверка идентификатора

    def check(self, vid: int, s: FetchSettings) -> tuple[Outcome, dict | None]:
        """Возвращает (итог, метаданные). Для найденной страницы итог — found."""
        url = s.url_template.replace("{id}", str(vid))
        retries = waits = 0
        while True:
            if self.stop.is_set():
                raise Stop()
            try:
                resp = self._get(url, s)
            except NetworkError:
                resp = None
            if resp is not None:
                self._note_status(resp.status)
                code = resp.status
                if code == 200:
                    try:
                        meta = resp.json()
                        if not isinstance(meta, dict):
                            raise ValueError
                    except ValueError:
                        return Outcome(vid, "failed", 200, "bad_metadata"), None
                    return Outcome(vid, "found", 200), meta
                if code == 404:
                    return Outcome(vid, "not_found", 404), None
                if code in (401, 403):
                    return Outcome(vid, "forbidden", code), None
                if code == 429:
                    waits += 1
                    delay = parse_retry_after(resp.headers.get("retry-after"))
                    self.gate.block_for(delay)  # притормаживаем все потоки, а не только этот
                    if waits > s.max_rate_limit_waits:
                        return Outcome(vid, "rate_limited", 429, "rate_limited"), None
                    self._pause(delay)
                    continue
                if code < 500:
                    return Outcome(vid, "failed", code, "unexpected_status"), None
            # сеть или 5xx — повторяем с задержкой
            retries += 1
            if retries > s.max_retries:
                return Outcome(vid, "failed", resp.status if resp else None, "http_5xx" if resp else "network"), None
            self._pause(backoff_delay(retries))

    # ------------------------------------------------------------ загрузка

    def target_path(self, vid: int, meta: dict, s: FetchSettings) -> str:
        return safe_join(s.storage_path, video_filename(vid, meta.get("title")))

    def download(self, vid: int, meta: dict, s: FetchSettings) -> Outcome:
        """Скачивает файл во временный `.part` с докачкой, проверяет размер и SHA-256, затем переименовывает."""
        url = meta.get("download_url")
        if not url or not meta.get("downloadable", True):
            return Outcome(vid, "skipped", None, "not_downloadable")
        if not self._allowed_download_host(url, s):
            return Outcome(vid, "failed", None, "download_host_not_allowed")
        size = meta.get("size_bytes")
        digest = meta.get("sha256")
        try:
            final = self.target_path(vid, meta, s)
        except ValueError:
            return Outcome(vid, "failed", None, "unsafe_path")
        os.makedirs(os.path.dirname(final), exist_ok=True)

        if os.path.exists(final) and self._matches(final, size, digest):
            return Outcome(vid, "completed", 200)  # уже скачан раньше (повторная выдача блока)

        if size and s.min_free_bytes is not None:
            free = shutil.disk_usage(os.path.dirname(final)).free
            if free - size < s.min_free_bytes:
                return Outcome(vid, "failed", None, "disk_full")

        part = final + ".part"
        retries = waits = 0
        while True:
            if self.stop.is_set():
                raise Stop()
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if size is not None and have > size:
                os.remove(part)
                have = 0
            if size is not None and have == size:
                break
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                self.gate.wait()
                stream = open_stream(url, headers=headers, timeout=s.request_timeout)
            except NetworkError:
                stream = None
            if stream is not None:
                with stream:
                    self._note_status(stream.status)
                    code = stream.status
                    if code in (200, 206):
                        if have and code == 200:  # сервер проигнорировал Range — начинаем заново
                            have = 0
                        try:
                            self._write(part, stream, append=bool(have) and code == 206)
                            if size is None or os.path.getsize(part) == size:
                                break
                        except NetworkError:
                            pass  # оборвано: продолжим с того же места
                    elif code == 416:
                        os.remove(part) if os.path.exists(part) else None
                    elif code == 404:
                        return Outcome(vid, "failed", 404, "file_missing")
                    elif code in (401, 403):
                        return Outcome(vid, "forbidden", code, "file_forbidden")
                    elif code == 429:
                        waits += 1
                        delay = parse_retry_after(stream.headers.get("retry-after"))
                        self.gate.block_for(delay)
                        if waits > s.max_rate_limit_waits:
                            return Outcome(vid, "rate_limited", 429, "rate_limited")
                        self._pause(delay)
                        continue
                    elif code < 500:
                        return Outcome(vid, "failed", code, "unexpected_status")
            retries += 1
            if retries > s.max_retries:
                return Outcome(vid, "failed", None, "download_failed")
            self._pause(backoff_delay(retries))

        if not self._matches(part, size, digest):
            os.remove(part)  # повреждённый файл не оставляем
            return Outcome(vid, "failed", 200, "checksum_mismatch")
        os.replace(part, final)  # атомарное появление готового файла
        return Outcome(vid, "completed", 200)

    # ------------------------------------------------------------ внутреннее

    @staticmethod
    def _allowed_download_host(url: str, s: FetchSettings) -> bool:
        """Ссылка на файл обязана вести на тот же хост, что и шаблон задания."""
        return urlparse(url).hostname == urlparse(s.url_template).hostname

    def _write(self, path: str, stream, append: bool) -> None:
        with open(path, "ab" if append else "wb") as fh:
            for data in stream.chunks():
                if self.stop.is_set():
                    raise Stop()
                fh.write(data)
                if self.counters:
                    self.counters.add_bytes(len(data))
            fh.flush()

    @staticmethod
    def _matches(path: str, size: int | None, digest: str | None) -> bool:
        if size is not None and os.path.getsize(path) != size:
            return False
        if digest:
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
            return h.hexdigest() == digest
        return True


__all__ = ["Fetcher", "FetchSettings", "Outcome", "Stop", "json", "time"]
