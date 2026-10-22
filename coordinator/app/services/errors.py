"""Ошибки бизнес-логики; API превращает их в JSON {"error": code, "message": ...}."""

from __future__ import annotations


class ServiceError(Exception):
    status_code = 400
    code = "error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class NotFound(ServiceError):
    status_code = 404
    code = "not_found"


class Conflict(ServiceError):
    status_code = 409
    code = "conflict"


class Invalid(ServiceError):
    status_code = 422
    code = "invalid"
