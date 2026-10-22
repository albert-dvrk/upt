"""Mock-сервис видеохостинга (раздел 20 задания).

Целевой ресурс для системы: GET /{id}. Для каждого id результат детерминирован
(зависит от seed и id): страница найдена (200), отсутствует (404) или закрыта (403).
Поверх этого поведение меняется на лету через управляющий API /_control/*:
задержки, 429 с Retry-After, случайные 500, обрывы загрузки, медленная отдача файлов.

Файлы генерируются на лету (размер зависит от id), поддерживают Range и
имеют проверяемую контрольную сумму SHA-256, которую сервис публикует в метаданных.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import re
import time
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from functools import lru_cache

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

BLOCK = 65536

# Часть названий намеренно «неудобные»: проверяют нормализацию имён на стороне агента.
TITLES = [
    "Вводная лекция",
    "Lecture 01: Intro",
    "../../etc/passwd",
    "Отчёт: итоги / планы?.mp4",
    "CON",
    "Очень длинное название " + "x" * 300,
    "tab\tand\nnewline",
    "Видео ‮ с управляющими символами",
]


@dataclass
class Behavior:
    seed: int = 1
    forbidden_permille: int = 30
    found_permille: int = 200
    file_min_kb: int = 64
    file_max_kb: int = 2048
    delay_ms: int = 0  # задержка любого ответа
    chunk_delay_ms: int = 0  # задержка между блоками файла (медленная загрузка)
    server_error_permille: int = 0  # доля случайных 500
    drop_permille: int = 0  # доля загрузок, обрываемых на половине
    retry_after_seconds: int = 3
    rate_limited_until: float = 0.0  # unix-время, до которого отвечаем 429
    rate_limit_files: bool = True


class ConfigPatch(BaseModel):
    seed: int | None = None
    forbidden_permille: int | None = Field(default=None, ge=0, le=1000)
    found_permille: int | None = Field(default=None, ge=0, le=1000)
    file_min_kb: int | None = Field(default=None, ge=1)
    file_max_kb: int | None = Field(default=None, ge=1)
    delay_ms: int | None = Field(default=None, ge=0)
    chunk_delay_ms: int | None = Field(default=None, ge=0)
    server_error_permille: int | None = Field(default=None, ge=0, le=1000)
    drop_permille: int | None = Field(default=None, ge=0, le=1000)
    retry_after_seconds: int | None = Field(default=None, ge=0)
    rate_limit_files: bool | None = None


class RateLimitRequest(BaseModel):
    seconds: float = Field(default=10, ge=0)  # 0 — снять ограничение
    retry_after_seconds: int | None = Field(default=None, ge=0)


# ------------------------------------------------------------------ детерминированная «модель мира»


def _h(seed: int, vid: int, salt: str) -> int:
    digest = hashlib.sha256(f"{seed}:{vid}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def page_kind(b: Behavior, vid: int) -> str:
    """found | forbidden | missing — зависит только от seed, id и долей."""
    r = _h(b.seed, vid, "kind") % 1000
    if r < b.forbidden_permille:
        return "forbidden"
    if r < b.forbidden_permille + b.found_permille:
        return "found"
    return "missing"


def file_size(b: Behavior, vid: int) -> int:
    lo = b.file_min_kb * 1024
    hi = max(lo, b.file_max_kb * 1024)
    return lo + _h(b.seed, vid, "size") % (hi - lo + 1)


def title_for(b: Behavior, vid: int) -> str:
    return TITLES[_h(b.seed, vid, "title") % len(TITLES)]


def content_bytes(seed: int, vid: int, start: int, end: int) -> bytes:
    """Байты файла [start, end] включительно; содержимое детерминировано."""
    out = bytearray()
    first, last = start // BLOCK, end // BLOCK
    for i in range(first, last + 1):
        block = hashlib.shake_256(f"{seed}:{vid}:{i}".encode()).digest(BLOCK)
        lo = start - i * BLOCK if i == first else 0
        hi = end - i * BLOCK + 1 if i == last else BLOCK
        out += block[lo:hi]
    return bytes(out)


@lru_cache(maxsize=4096)
def full_sha256(seed: int, vid: int, size: int) -> str:
    h = hashlib.sha256()
    pos = 0
    while pos < size:
        stop = min(pos + BLOCK, size) - 1
        h.update(content_bytes(seed, vid, pos, stop))
        pos = stop + 1
    return h.hexdigest()


_RANGE_RE = re.compile(r"bytes=([0-9]*)-([0-9]*)")


def parse_http_range(header: str, size: int) -> tuple[int, int] | None | str:
    """(start, end) | None — заголовок игнорируем | "invalid" — 416."""
    m = _RANGE_RE.fullmatch(header.strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    a, b = m.groups()
    if a == "":  # суффикс: последние N байт
        n = int(b)
        if n == 0:
            return "invalid"
        return max(0, size - n), size - 1
    start = int(a)
    end = int(b) if b else size - 1
    if start >= size or end < start:
        return "invalid"
    return start, min(end, size - 1)


# ------------------------------------------------------------------ состояние и middleware


class MockState:
    def __init__(self, behavior: Behavior | None = None) -> None:
        self.behavior = behavior or Behavior()
        self.stats: Counter[int] = Counter()


class StatsMiddleware:
    """Считает коды ответов для всех запросов, кроме управляющего API."""

    def __init__(self, app, state: MockState) -> None:
        self.app = app
        self.state = state

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"].startswith("/_control"):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                self.state.stats[message["status"]] += 1
            await send(message)

        await self.app(scope, receive, send_wrapper)


async def _gate(state: MockState, kind: str) -> Response | None:
    """Общие для страниц и файлов эффекты: задержка, 429, случайный 500."""
    b = state.behavior
    if b.delay_ms:
        await asyncio.sleep(b.delay_ms / 1000)
    if (kind == "page" or b.rate_limit_files) and time.time() < b.rate_limited_until:
        return JSONResponse(
            {"error": "rate_limited"},
            status_code=429,
            headers={"Retry-After": str(b.retry_after_seconds)},
        )
    if b.server_error_permille and random.randrange(1000) < b.server_error_permille:
        return JSONResponse({"error": "internal"}, status_code=500)
    return None


# ------------------------------------------------------------------ приложение


def create_app(behavior: Behavior | None = None) -> FastAPI:
    state = MockState(behavior)
    app = FastAPI(title="videofetch mock hosting")
    app.state.mock = state
    app.add_middleware(StatsMiddleware, state=state)

    # ----- управляющий API (в compose-сети; наружу не публиковать в боевой среде)

    @app.get("/_control/state")
    def get_state() -> dict:
        return asdict(state.behavior)

    @app.post("/_control/config")
    def patch_config(patch: ConfigPatch) -> dict:
        for key, value in patch.model_dump(exclude_none=True).items():
            setattr(state.behavior, key, value)
        return asdict(state.behavior)

    @app.post("/_control/rate_limit")
    def set_rate_limit(req: RateLimitRequest) -> dict:
        b = state.behavior
        if req.retry_after_seconds is not None:
            b.retry_after_seconds = req.retry_after_seconds
        b.rate_limited_until = time.time() + req.seconds if req.seconds > 0 else 0.0
        return asdict(b)

    @app.get("/_control/stats")
    def get_stats() -> dict:
        return {"status_counts": {str(k): v for k, v in sorted(state.stats.items())}}

    @app.post("/_control/reset")
    def reset() -> dict:
        seed = state.behavior.seed
        state.behavior = Behavior(seed=seed)
        state.stats.clear()
        return asdict(state.behavior)

    # ----- «видеохостинг»

    @app.get("/{video_id:int}")
    async def page(video_id: int, request: Request) -> Response:
        early = await _gate(state, "page")
        if early is not None:
            return early
        b = state.behavior
        kind = page_kind(b, video_id)
        if kind == "forbidden":
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if kind == "missing":
            return JSONResponse({"error": "not_found"}, status_code=404)
        size = file_size(b, video_id)
        base = str(request.base_url).rstrip("/")
        return JSONResponse(
            {
                "id": video_id,
                "title": title_for(b, video_id),
                "page_url": f"{base}/{video_id}",
                "download_url": f"{base}/files/{video_id}/video.mp4",
                "mime_type": "video/mp4",
                "size_bytes": size,
                "sha256": full_sha256(b.seed, video_id, size),
                "downloadable": True,
            }
        )

    @app.api_route("/files/{video_id:int}/video.mp4", methods=["GET", "HEAD"])
    async def download(video_id: int, request: Request) -> Response:
        early = await _gate(state, "file")
        if early is not None:
            return early
        b = state.behavior
        kind = page_kind(b, video_id)
        if kind == "forbidden":
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if kind == "missing":
            return JSONResponse({"error": "not_found"}, status_code=404)

        size = file_size(b, video_id)
        headers = {
            "Accept-Ranges": "bytes",
            "ETag": f'"{full_sha256(b.seed, video_id, size)}"',
            "Content-Type": "video/mp4",
        }
        start, end, status = 0, size - 1, 200
        range_header = request.headers.get("range")
        if range_header:
            parsed = parse_http_range(range_header, size)
            if parsed == "invalid":
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            if isinstance(parsed, tuple):
                start, end = parsed
                status = 206
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(end - start + 1)

        if request.method == "HEAD":
            return Response(status_code=status, headers=headers)

        truncate_at = None
        if b.drop_permille and random.randrange(1000) < b.drop_permille:
            truncate_at = start + (end - start + 1) // 2

        async def body() -> AsyncIterator[bytes]:
            pos = start
            while pos <= end:
                if truncate_at is not None and pos >= truncate_at:
                    raise ConnectionAbortedError("mock: соединение оборвано")
                stop = min(pos + BLOCK - 1, end)
                yield content_bytes(b.seed, video_id, pos, stop)
                pos = stop + 1
                if b.chunk_delay_ms:
                    await asyncio.sleep(b.chunk_delay_ms / 1000)

        return StreamingResponse(body(), status_code=status, headers=headers)

    return app


__all__ = ["Behavior", "create_app", "page_kind", "file_size", "full_sha256", "content_bytes", "math"]
