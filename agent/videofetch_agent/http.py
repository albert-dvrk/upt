"""Минимальный HTTP-клиент поверх стандартной библиотеки (на рабочих серверах нет лишних зависимостей)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator


class NetworkError(Exception):
    """Сеть недоступна, таймаут или обрыв соединения (можно повторить)."""


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self):
        return json.loads(self.body.decode("utf-8"))


def _headers(msg) -> dict[str, str]:
    return {k.lower(): v for k, v in msg.items()}


def request(method: str, url: str, *, headers: dict | None = None, body: bytes | None = None, timeout: float = 10, context=None) -> Response:
    """Обычный запрос. Коды 4xx/5xx возвращаются как Response, а не исключением."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            return Response(resp.status, _headers(resp.headers), resp.read())
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read()
        except Exception:  # noqa: BLE001
            data = b""
        return Response(exc.code, _headers(exc.headers), data)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise NetworkError(str(exc)) from exc


def json_request(method: str, url: str, payload=None, *, timeout: float = 10, headers: dict | None = None, context=None) -> Response:
    body = None
    hdrs = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    return request(method, url, headers=hdrs, body=body, timeout=timeout, context=context)


class StreamResponse:
    """Потоковый ответ; используется как контекстный менеджер."""

    def __init__(self, resp, status: int, headers: dict[str, str]) -> None:
        self._resp = resp
        self.status = status
        self.headers = headers

    def chunks(self, size: int = 65536) -> Iterator[bytes]:
        try:
            while True:
                data = self._resp.read(size)
                if not data:
                    return
                yield data
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise NetworkError(str(exc)) from exc
        except Exception as exc:  # IncompleteRead и подобные
            raise NetworkError(repr(exc)) from exc

    def close(self) -> None:
        self._resp.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_stream(url: str, *, headers: dict | None = None, timeout: float = 20) -> StreamResponse:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return StreamResponse(resp, resp.status, _headers(resp.headers))
    except urllib.error.HTTPError as exc:
        return StreamResponse(exc, exc.code, _headers(exc.headers))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        raise NetworkError(str(exc)) from exc
