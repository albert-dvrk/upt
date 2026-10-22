"""Рабочие серверы: учёт, регистрация агента, heartbeat."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Job, JobWorker, RangeChunk, Worker, WorkerMetric
from app.schemas import MetricsIn, WorkerCreate
from app.services import jobs
from app.services.errors import Conflict, NotFound
from app.services.events import log_event
from app.statuses import ChunkStatus, JobStatus, WorkerStatus
from app.timeutil import utcnow

PATCHABLE = ("host", "ssh_port", "ssh_user", "ssh_key_ref", "threads", "storage_path", "min_free_space_gb")


def get_worker(session: Session, worker_id: int) -> Worker:
    worker = session.get(Worker, worker_id)
    if worker is None:
        raise NotFound(f"Сервер {worker_id} не найден", "worker_not_found")
    return worker


def list_workers(session: Session) -> list[Worker]:
    return list(session.scalars(select(Worker).order_by(Worker.name)).all())


def create_worker(session: Session, data: WorkerCreate) -> Worker:
    if session.scalar(select(Worker.id).where(Worker.name == data.name)) is not None:
        raise Conflict(f"Сервер с именем {data.name} уже есть", "worker_exists")
    worker = Worker(
        name=data.name,
        host=data.host,
        ssh_port=data.ssh_port,
        ssh_user=data.ssh_user,
        auth_method=data.auth_method,
        ssh_key_ref=data.ssh_key_ref,
        threads=data.threads,
        storage_path=data.storage_path,
        min_free_space_gb=data.min_free_space_gb,
        status=WorkerStatus.OFFLINE.value,
        enabled=True,
    )
    session.add(worker)
    try:
        session.flush()
    except IntegrityError as exc:  # гонка двух одинаковых запросов
        raise Conflict(f"Сервер с именем {data.name} уже есть", "worker_exists") from exc
    log_event(session, "worker_created", worker_id=worker.id, result="ok")
    return worker


def update_worker(session: Session, worker_id: int, changes: dict[str, Any]) -> Worker:
    worker = get_worker(session, worker_id)
    for key in PATCHABLE:
        if key in changes:
            setattr(worker, key, changes[key])
    if "enabled" in changes:
        worker.enabled = changes["enabled"]
        if worker.enabled:
            worker.status = WorkerStatus.OFFLINE.value  # станет online после heartbeat
        else:
            worker.status = WorkerStatus.DISABLED.value
            released = jobs.release_leases(session, worker_id=worker.id)
            log_event(session, "worker_disabled", worker_id=worker.id, message=f"released_chunks={released}")
    session.flush()
    log_event(session, "worker_updated", worker_id=worker.id, result="ok", message=f"fields={sorted(changes)}")
    return worker


def register_agent(
    session: Session,
    name: str,
    agent_version: str,
    resources: dict[str, Any] | None,
    *,
    now: dt.datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Агент представляется координатору. Сервер должен быть заранее заведён администратором."""
    settings = settings or get_settings()
    now = now or utcnow()
    worker = session.scalars(select(Worker).where(Worker.name == name)).first()
    if worker is None:
        raise NotFound(f"Сервер {name} не заведён в координаторе", "worker_unknown")
    if not worker.enabled:
        raise Conflict("Сервер отключён", "worker_disabled")
    worker.status = WorkerStatus.ONLINE.value
    worker.agent_version = agent_version
    worker.last_heartbeat_at = now
    if resources is not None:
        worker.resources = resources
    session.flush()
    log_event(session, "agent_registered", worker_id=worker.id, result="ok", message=f"version={agent_version}")
    return {
        "worker_id": worker.id,
        "name": worker.name,
        "threads": worker.threads,
        "storage_path": worker.storage_path,
        "min_free_space_gb": worker.min_free_space_gb,
        "lease_seconds": settings.lease_seconds,
        "heartbeat_interval_seconds": settings.heartbeat_interval_seconds,
    }


def heartbeat(
    session: Session,
    worker_id: int,
    *,
    agent_version: str | None = None,
    resources: dict[str, Any] | None = None,
    metrics: MetricsIn | None = None,
    now: dt.datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Подтверждает, что агент жив: продлевает его аренды, принимает ресурсы и метрики."""
    settings = settings or get_settings()
    now = now or utcnow()
    worker = get_worker(session, worker_id)
    if not worker.enabled:
        raise Conflict("Сервер отключён", "worker_disabled")
    worker.status = WorkerStatus.ONLINE.value
    worker.last_heartbeat_at = now
    if agent_version:
        worker.agent_version = agent_version
    if resources is not None:
        worker.resources = resources
    if metrics is not None:
        values = metrics.model_dump(exclude_none=True, exclude={"runtime"})
        if values:
            session.add(WorkerMetric(worker_id=worker.id, **values))
        if metrics.runtime is not None:  # состояние «здесь и сейчас»: хранится в ресурсах сервера, а не в истории метрик
            merged = dict(worker.resources or {})
            merged["runtime"] = metrics.runtime
            worker.resources = merged

    jobs.bulk_update(
        session,
        update(RangeChunk)
        .where(RangeChunk.leased_by == worker_id, RangeChunk.status == ChunkStatus.LEASED.value)
        .values(lease_expires_at=now + dt.timedelta(seconds=settings.lease_seconds)),
    )

    worker = get_worker(session, worker_id)  # после bulk_update состояние сессии сброшено
    rows = session.execute(
        select(Job, JobWorker)
        .join(JobWorker, JobWorker.job_id == Job.id)
        .where(
            JobWorker.worker_id == worker_id,
            JobWorker.enabled.is_(True),
            Job.status.in_((JobStatus.RUNNING.value, JobStatus.PAUSING.value)),
        )
        .order_by(Job.id)
    ).all()
    return {
        "server_time": now,
        "lease_seconds": settings.lease_seconds,
        "jobs": [
            {
                "job_id": job.id,
                "status": job.status,
                "threads": jobs.effective_threads(job, jw, worker),
                "rate_limit_rps": jobs.effective_rate_limit(session, job, jw),
            }
            for job, jw in rows
        ],
    }


def list_metrics(session: Session, worker_id: int, *, limit: int) -> list[WorkerMetric]:
    get_worker(session, worker_id)
    stmt = (
        select(WorkerMetric)
        .where(WorkerMetric.worker_id == worker_id)
        .order_by(WorkerMetric.id.desc())
        .limit(limit)
    )
    return list(session.scalars(stmt).all())
