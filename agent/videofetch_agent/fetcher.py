"""Проверка идентификатора и загрузка файла: повторы, докачка, проверка SHA-256."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .http import NetworkError, open_stream, request
from .util import RateGate, backoff_delay, parse_retry_after, safe_join


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
    media: dict | None = None  # метаданные материала (для таблицы media_metadata)
    file: dict | None = None  # скачанный файл: относительный путь, размер, SHA-256

    def as_item(self) -> dict:
        base = {"external_id": self.external_id, "status": self.status, "http_status": self.http_status,
                "error_code": self.error_code, "media": self.media, "file": self.file}
        return {k: v for k, v in base.items() if v is not None}


MIME_EXT = {"video/mp4": "mp4", "video/webm": "webm", "video/x-matroska": "mkv", "video/quicktime": "mov",
            "video/mpeg": "mpg", "video/ogg": "ogv", "video/x-msvideo": "avi"}
ID_PREFIX_DIVISOR = 1_000_000  # id 200673499 -> каталог «200», как в примере задания


def media_from(vid: int, meta: dict, s: "FetchSettings") -> dict:
    """Приводит ответ сервиса к формату MediaIn координатора."""
    sha = meta.get("sha256")
    size = meta.get("size_bytes")
    return {k: v for k, v in {
        "title": str(meta["title"])[:1000] if meta.get("title") is not None else None,
        "page_url": str(meta.get("page_url") or s.url_template.replace("{id}", str(vid)))[:1024],
        "download_url": str(meta["download_url"])[:2048] if meta.get("download_url") else None,
        "mime_type": str(meta["mime_type"])[:128] if meta.get("mime_type") else None,
        "size_bytes": size if isinstance(size, int) and size >= 0 else None,
        "checksum_sha256": sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) else None,
    }.items() if v is not None}


def extension_for(meta: dict) -> str:
    ext = MIME_EXT.get(str(meta.get("mime_type") or "").lower())
    if ext:
        return ext
    m = re.search(r"\.([A-Za-z0-9]{1,5})(?:$|\?)", urlparse(str(meta.get("download_url") or "")).path)
    return m.group(1).lower() if m else "bin"


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
        """{storage}/{id-prefix}/{id}/video.{ext}: имя каталога — идентификатор, название материала в пути не участвует."""
        return safe_join(s.storage_path, str(vid // ID_PREFIX_DIVISOR), str(vid), f"video.{extension_for(meta)}")

    def download(self, vid: int, meta: dict, s: FetchSettings) -> Outcome:
        """Скачивает файл во временный `.part` с докачкой, проверяет размер и SHA-256, затем переименовывает.

        В любом исходе возвращает метаданные материала; при успехе — ещё и описание файла, а рядом
        с видео записывается metadata.json.
        """
        out = self._download(vid, meta, s)
        out.media = media_from(vid, meta, s)
        return out

    def _download(self, vid: int, meta: dict, s: FetchSettings) -> Outcome:
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
            return self._finish(vid, meta, s, final)  # уже скачан раньше (повторная выдача блока)

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
        return self._finish(vid, meta, s, final)

    # ------------------------------------------------------------ внутреннее

    def _finish(self, vid: int, meta: dict, s: FetchSettings, final: str) -> Outcome:
        """Считает итоговый SHA-256, пишет metadata.json (атомарно) и формирует описание файла."""
        h = hashlib.sha256()
        with open(final, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        digest = h.hexdigest()
        size = os.path.getsize(final)
        root = os.path.realpath(s.storage_path)
        rel = os.path.relpath(final, root).replace(os.sep, "/")
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        info = {
            "id": vid, **{k: v for k, v in media_from(vid, meta, s).items()},
            "file": os.path.basename(final), "size_bytes": size, "sha256": digest,
            "downloaded_at": now, "status": "completed",
        }
        mpath = os.path.join(os.path.dirname(final), "metadata.json")
        with open(mpath + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(info, fh, ensure_ascii=False, indent=2)
        os.replace(mpath + ".tmp", mpath)
        mime = meta.get("mime_type")
        file = {"storage_path": rel, "size_bytes": size, "checksum_sha256": digest}
        if mime:
            file["mime_type"] = str(mime)[:128]
        out = Outcome(vid, "completed", 200)
        out.file = file
        return out

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


__all__ = ["Fetcher", "FetchSettings", "Outcome", "Stop", "media_from", "extension_for", "field", "time"]
