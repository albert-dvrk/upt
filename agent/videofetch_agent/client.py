"""Клиент API координатора (протокол аренды блоков)."""

from __future__ import annotations

import ssl
from typing import Any

from .http import NetworkError, json_request


class LeaseLost(Exception):
    """Аренда блока потеряна: результаты этого блока больше отправлять нельзя."""


class CoordinatorError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class CoordinatorClient:
    def __init__(self, base_url: str, worker_name: str, *, token: str | None = None, timeout: float = 10, ca_file: str | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.name = worker_name
        self.timeout = timeout
        self.token = token
        # Свой сертификат доверяется только для обращений к координатору, не к целевому серверу.
        self.context = ssl.create_default_context(cafile=ca_file) if ca_file else None
        self.worker_id: int | None = None

    def _call(self, method: str, path: str, payload: Any = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else None
        resp = json_request(method, self.base + path, payload, timeout=self.timeout, headers=headers, context=self.context)
        if resp.status >= 400:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            code, message = data.get("error", "http_error"), data.get("message", "")
            if resp.status == 409 and code in ("lease_lost", "worker_disabled"):
                raise LeaseLost(code)
            raise CoordinatorError(resp.status, code, message)
        return resp.json() if resp.body else {}

    def _worker(self, tail: str, payload: Any = None) -> dict:
        return self._call("POST", f"/api/workers/{self.worker_id}/{tail}", payload)

    def register(self, version: str, resources: dict | None = None) -> dict:
        data = self._call("POST", "/api/workers/register", {"name": self.name, "agent_version": version, "resources": resources})
        self.worker_id = data["worker_id"]
        return data

    def heartbeat(self, metrics: dict | None = None, resources: dict | None = None) -> dict:
        return self._worker("heartbeat", {"metrics": metrics, "resources": resources})

    def lease(self) -> dict:
        return self._worker("lease")

    def progress(self, chunk_id: int, next_id: int, items: list[dict]) -> dict:
        return self._worker("progress", {"chunk_id": chunk_id, "next_id": next_id, "items": items})

    def complete(self, chunk_id: int, items: list[dict]) -> dict:
        return self._worker("complete", {"chunk_id": chunk_id, "items": items})

    def fail(self, chunk_id: int, error_code: str, message: str | None = None, retryable: bool = True) -> dict:
        return self._worker("fail", {"chunk_id": chunk_id, "error_code": error_code, "message": (message or "")[:2000] or None, "retryable": retryable})


__all__ = ["CoordinatorClient", "CoordinatorError", "LeaseLost", "NetworkError"]
