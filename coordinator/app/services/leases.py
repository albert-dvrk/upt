"""Аренда блоков диапазона (разделы 6.1–6.3, 16 задания).

Модель доставки — at least once: блок можно обработать повторно, поэтому сохранение
результатов идемпотентно (items.upsert_items), а повторные запросы агента безопасны:
повторный lease возвращает уже арендованный блок, повторный complete — успех.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Item, Job, JobWorker, RangeChunk, Worker
from app.ranges import next_chunk
from app.schemas import ItemIn
from app.services import jobs
from app.services.errors import Conflict, Invalid, NotFound
from app.services.events import log_event
from app.services.items import upsert_items
from app.statuses import ChunkStatus, JobStatus, WorkerStatus
from app.timeutil import as_utc, utcnow


def _lease_delta(settings: Settings) -> dt.timedelta:
    return dt.timedelta(seconds=settings.lease_seconds)


def _capacity_reason(worker: Worker) -> str | None:
    """Выдача блоков приостанавливается, если на сервере заканчивается место."""
    free = (worker.resources or {}).get("disk_free_bytes")
    if isinstance(free, (int, float)) and free < worker.min_free_space_gb * 1024**3:
        return "disk_low"
    return None


def chunk_settings(session: Session, job: Job, worker: Worker) -> dict[str, Any]:
    jw = session.get(JobWorker, (job.id, worker.id))
    return {
        "url_template": (job.config or {}).get("url_template"),
        "threads": jobs.effective_threads(job, jw, worker),
        "rate_limit_rps": jobs.effective_rate_limit(session, job, jw),
        "request_timeout_seconds": job.request_timeout_seconds,
        "max_retries": job.max_retries,
        "storage_path": worker.storage_path,
    }


def _payload(session: Session, job: Job, chunk: RangeChunk, worker: Worker) -> dict[str, Any]:
    return {
        "chunk_id": chunk.id,
        "job_id": job.id,
        "range_start": chunk.range_start,
        "range_end": chunk.range_end,
        "next_id": chunk.next_id,
        "attempt": chunk.attempt,
        "lease_expires_at": chunk.lease_expires_at,
        "settings": chunk_settings(session, job, worker),
    }


def _own_active_chunk(session: Session, worker_id: int) -> RangeChunk | None:
    """Блок, который этот сервер уже арендует (повторный запрос или перезапуск агента)."""
    stmt = (
        select(RangeChunk)
        .join(Job, Job.id == RangeChunk.job_id)
        .where(
            RangeChunk.leased_by == worker_id,
            RangeChunk.status == ChunkStatus.LEASED.value,
            Job.status.in_((JobStatus.RUNNING.value, JobStatus.PAUSING.value)),
        )
        .order_by(RangeChunk.id)
        .limit(1)
    )
    return session.scalars(stmt).first()


def _claim_existing(
    session: Session, job: Job, worker: Worker, now: dt.datetime, settings: Settings
) -> RangeChunk | None:
    """Атомарно берёт свободный блок (или блок с истёкшей арендой): FOR UPDATE SKIP LOCKED."""
    for _ in range(50):
        stmt = (
            select(RangeChunk)
            .where(
                RangeChunk.job_id == job.id,
                or_(
                    RangeChunk.status == ChunkStatus.PENDING.value,
                    and_(
                        RangeChunk.status == ChunkStatus.LEASED.value,
                        RangeChunk.lease_expires_at < now,
                    ),
                ),
            )
            .order_by(RangeChunk.range_start)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        chunk = session.scalars(stmt).first()
        if chunk is None:
            return None
        if chunk.attempt >= settings.max_chunk_attempts:
            chunk.status = ChunkStatus.FAILED.value
            chunk.leased_by = None
            chunk.lease_expires_at = None
            session.flush()
            log_event(
                session,
                "chunk_failed",
                level="ERROR",
                job_id=job.id,
                chunk_id=chunk.id,
                result="failed",
                error_code="attempts_exhausted",
            )
            continue
        previous = chunk.leased_by
        chunk.status = ChunkStatus.LEASED.value
        chunk.leased_by = worker.id
        chunk.lease_expires_at = now + _lease_delta(settings)
        chunk.attempt += 1
        session.flush()
        log_event(
            session,
            "lease_reclaimed" if chunk.attempt > 1 else "lease_granted",
            job_id=job.id,
            worker_id=worker.id,
            chunk_id=chunk.id,
            result="ok",
            message=f"attempt={chunk.attempt} previous_owner={previous}",
        )
        return chunk
    return None


def _create_next(
    session: Session, job: Job, worker: Worker, now: dt.datetime, settings: Settings
) -> RangeChunk | None:
    """Нарезает следующий блок диапазона. Блокировка строки задания исключает двойную нарезку."""
    session.execute(select(Job.id).where(Job.id == job.id).with_for_update())
    last_end = session.scalar(select(func.max(RangeChunk.range_end)).where(RangeChunk.job_id == job.id))
    bounds = next_chunk(jobs.effective_range(job), job.chunk_size, last_end)
    if bounds is None:
        return None
    chunk = RangeChunk(
        job_id=job.id,
        range_start=bounds[0],
        range_end=bounds[1],
        next_id=bounds[0],
        status=ChunkStatus.LEASED.value,
        leased_by=worker.id,
        lease_expires_at=now + _lease_delta(settings),
        attempt=1,
    )
    session.add(chunk)
    session.flush()
    log_event(
        session,
        "lease_granted",
        job_id=job.id,
        worker_id=worker.id,
        chunk_id=chunk.id,
        result="ok",
        message=f"range={bounds[0]}-{bounds[1]}",
    )
    return chunk


def lease_chunk(
    session: Session, worker_id: int, *, now: dt.datetime | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """Выдаёт серверу блок. Ответ: {"chunk": {...} | None, "reason": None | "disk_low" | ...}."""
    settings = settings or get_settings()
    now = now or utcnow()
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise NotFound(f"Сервер {worker_id} не найден", "worker_not_found")
    if not worker.enabled:
        raise Conflict("Сервер отключён", "worker_disabled")
    worker.status = WorkerStatus.ONLINE.value
    worker.last_heartbeat_at = now

    own = _own_active_chunk(session, worker_id)
    if own is not None:  # идемпотентность: повторный запрос возвращает тот же блок
        own.lease_expires_at = now + _lease_delta(settings)
        session.flush()
        job = session.get(Job, own.job_id)
        return {"chunk": _payload(session, job, own, worker), "reason": None}

    reason = _capacity_reason(worker)
    if reason:
        return {"chunk": None, "reason": reason}

    running = session.scalars(
        select(Job)
        .join(JobWorker, JobWorker.job_id == Job.id)
        .where(
            Job.status == JobStatus.RUNNING.value,
            JobWorker.worker_id == worker_id,
            JobWorker.enabled.is_(True),
        )
        .order_by(Job.id)
    ).all()
    if not running:
        return {"chunk": None, "reason": "no_active_jobs"}

    for job in running:
        chunk = _claim_existing(session, job, worker, now, settings) or _create_next(
            session, job, worker, now, settings
        )
        if chunk is not None:
            return {"chunk": _payload(session, job, chunk, worker), "reason": None}
        jobs.settle_job(session, job, now=now)
    return {"chunk": None, "reason": "no_work"}


# ------------------------------------------------------------------ прогресс, завершение, ошибки


def _get_chunk(session: Session, chunk_id: int) -> RangeChunk:
    stmt = (
        select(RangeChunk)
        .where(RangeChunk.id == chunk_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    chunk = session.scalars(stmt).first()
    if chunk is None:
        raise NotFound(f"Блок {chunk_id} не найден", "chunk_not_found")
    return chunk


def _ensure_owner(chunk: RangeChunk, worker_id: int) -> None:
    if chunk.status != ChunkStatus.LEASED.value or chunk.leased_by != worker_id:
        raise Conflict("Аренда блока потеряна или принадлежит другому серверу", "lease_lost")


def report_progress(
    session: Session,
    worker_id: int,
    chunk_id: int,
    next_id: int,
    items: list[ItemIn],
    *,
    now: dt.datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Сохраняет результаты, двигает контрольную точку (только вперёд) и продлевает аренду."""
    settings = settings or get_settings()
    now = now or utcnow()
    chunk = _get_chunk(session, chunk_id)
    _ensure_owner(chunk, worker_id)
    if not (chunk.range_start <= next_id <= chunk.range_end + 1):
        raise Invalid("next_id вне границ блока", "bad_next_id")

    upsert_items(session, chunk, worker_id, items, now)
    chunk.next_id = max(chunk.next_id, next_id)
    chunk.lease_expires_at = now + _lease_delta(settings)
    session.flush()

    job = session.get(Job, chunk.job_id)
    worker = session.get(Worker, worker_id)
    return {
        "lease_expires_at": chunk.lease_expires_at,
        "job_status": job.status,
        "settings": chunk_settings(session, job, worker),
    }


def complete_chunk(
    session: Session,
    worker_id: int,
    chunk_id: int,
    items: list[ItemIn],
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Завершает блок. Требует результатов для каждого идентификатора блока (без пропусков)."""
    now = now or utcnow()
    chunk = _get_chunk(session, chunk_id)
    if chunk.status == ChunkStatus.COMPLETED.value and chunk.leased_by == worker_id:
        job = session.get(Job, chunk.job_id)  # повторное сообщение — не ошибка
        return {"job_status": job.status}
    _ensure_owner(chunk, worker_id)

    upsert_items(session, chunk, worker_id, items, now)
    size = chunk.range_end - chunk.range_start + 1
    have = session.scalar(
        select(func.count())
        .select_from(Item)
        .where(
            Item.job_id == chunk.job_id,
            Item.external_id >= chunk.range_start,
            Item.external_id <= chunk.range_end,
        )
    )
    if (have or 0) < size:
        raise Conflict(f"В блоке не хватает результатов: {size - (have or 0)}", "chunk_incomplete")

    chunk.next_id = chunk.range_end + 1
    chunk.status = ChunkStatus.COMPLETED.value
    chunk.lease_expires_at = None
    session.flush()
    log_event(
        session,
        "chunk_completed",
        job_id=chunk.job_id,
        worker_id=worker_id,
        chunk_id=chunk.id,
        result="completed",
    )
    job = session.get(Job, chunk.job_id)
    jobs.settle_job(session, job, now=now)
    return {"job_status": job.status}


def fail_chunk(
    session: Session,
    worker_id: int,
    chunk_id: int,
    error_code: str,
    message: str | None,
    retryable: bool,
    *,
    now: dt.datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Блок возвращается в очередь; после исчерпания попыток (или при retryable=false) — failed."""
    settings = settings or get_settings()
    now = now or utcnow()
    chunk = _get_chunk(session, chunk_id)
    _ensure_owner(chunk, worker_id)

    exhausted = (not retryable) or chunk.attempt >= settings.max_chunk_attempts
    chunk.status = ChunkStatus.FAILED.value if exhausted else ChunkStatus.PENDING.value
    chunk.leased_by = None
    chunk.lease_expires_at = None
    session.flush()
    log_event(
        session,
        "chunk_failed",
        level="ERROR" if exhausted else "WARN",
        job_id=chunk.job_id,
        worker_id=worker_id,
        chunk_id=chunk.id,
        result=chunk.status,
        error_code=error_code,
        message=message,
    )
    job = session.get(Job, chunk.job_id)
    jobs.settle_job(session, job, now=now)
    return {"chunk_status": chunk.status, "job_status": job.status}


# ------------------------------------------------------------------ фоновая проверка


def reap_expired(
    session: Session, *, now: dt.datetime | None = None, settings: Settings | None = None
) -> dict[str, int]:
    """Возвращает в очередь блоки молчащих серверов и с истёкшей арендой, применяет лимит времени задания."""
    settings = settings or get_settings()
    now = now or utcnow()
    stats = {"workers_offline": 0, "chunks_released": 0, "jobs_stopped": 0, "jobs_settled": 0}

    cutoff = now - dt.timedelta(seconds=settings.heartbeat_timeout_seconds)
    stale = session.scalars(
        select(Worker).where(
            Worker.status == WorkerStatus.ONLINE.value,
            Worker.last_heartbeat_at < cutoff,
        )
    ).all()
    for worker in stale:
        worker.status = WorkerStatus.OFFLINE.value
        released = jobs.release_leases(session, worker_id=worker.id)
        stats["workers_offline"] += 1
        stats["chunks_released"] += released
        log_event(
            session,
            "worker_offline",
            level="WARN",
            worker_id=worker.id,
            result="heartbeat_timeout",
            message=f"released_chunks={released}",
        )

    expired = jobs.bulk_update(
        session,
        update(RangeChunk)
        .where(RangeChunk.status == ChunkStatus.LEASED.value, RangeChunk.lease_expires_at < now)
        .values(status=ChunkStatus.PENDING.value, leased_by=None, lease_expires_at=None),
    )
    if expired:
        stats["chunks_released"] += expired
        log_event(session, "leases_expired", level="WARN", result="released", message=f"chunks={expired}")

    active = session.scalars(
        select(Job).where(
            Job.status.in_(
                (JobStatus.RUNNING.value, JobStatus.PAUSING.value, JobStatus.PAUSED.value)
            )
        )
    ).all()
    for job in active:
        started = as_utc(job.started_at)
        if (
            job.max_runtime_seconds
            and started is not None
            and started + dt.timedelta(seconds=job.max_runtime_seconds) <= now
        ):
            jobs.stop(session, job, now=now)
            log_event(session, "job_time_limit", job_id=job.id, result="stopped")
            stats["jobs_stopped"] += 1
            continue
        before = job.status
        jobs.settle_job(session, job, now=now)
        if job.status != before:
            stats["jobs_settled"] += 1
    return stats
