"""Запись структурированных событий (раздел 15 задания). Секреты сюда не попадают."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Event


def log_event(
    session: Session,
    operation: str,
    *,
    level: str = "INFO",
    component: str = "coordinator",
    result: str | None = None,
    job_id: int | None = None,
    worker_id: int | None = None,
    chunk_id: int | None = None,
    external_id: int | None = None,
    error_code: str | None = None,
    message: str | None = None,
) -> None:
    session.add(
        Event(
            level=level,
            component=component,
            job_id=job_id,
            worker_id=worker_id,
            chunk_id=chunk_id,
            external_id=external_id,
            operation=operation,
            result=result,
            error_code=error_code,
            message=message[:1000] if message else None,
        )
    )
