"""Задания: создание, жизненный цикл, настройки, статистика (разделы 10, 11.3, 19)."""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Event, Item, Job, JobWorker, RangeChunk, Worker
from app.ranges import IdRange, RangeError, from_to, next_chunk, parse_range
from app.schemas import JobCreate, JobOut
from app.services.errors import Conflict, Invalid, NotFound
from app.services.events import log_event
from app.statuses import (
    TERMINAL_JOB_STATUSES,
    ChunkStatus,
    InvalidTransition,
    ItemStatus,
    JobStatus,
    WorkerStatus,
    ensure_transition,
)
from app.timeutil import utcnow

NULLABLE_PATCH_FIELDS = {"threads", "global_rate_limit_rps"}


# ------------------------------------------------------------------ служебное


def bulk_update(session: Session, stmt) -> int:
    """Массовый UPDATE. Состояние сессии после него сбрасывается и перечитывается лениво."""
    session.flush()
    result = session.execute(stmt.execution_options(synchronize_session=False))
    session.expire_all()
    return result.rowcount or 0


def release_leases(session: Session, *, job_id: int | None = None, worker_id: int | None = None) -> int:
    """Возвращает арендованные блоки в очередь (контрольная точка next_id сохраняется)."""
    stmt = update(RangeChunk).where(RangeChunk.status == ChunkStatus.LEASED.value)
    if job_id is not None:
        stmt = stmt.where(RangeChunk.job_id == job_id)
    if worker_id is not None:
        stmt = stmt.where(RangeChunk.leased_by == worker_id)
    stmt = stmt.values(status=ChunkStatus.PENDING.value, leased_by=None, lease_expires_at=None)
    return bulk_update(session, stmt)


def get_job(session: Session, job_id: int, *, lock: bool = False) -> Job:
    stmt = select(Job).where(Job.id == job_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    job = session.scalars(stmt).first()
    if job is None:
        raise NotFound(f"Задание {job_id} не найдено", "job_not_found")
    return job


def effective_range(job: Job) -> IdRange:
    """Диапазон с учётом ограничения max_ids (для открытого диапазона)."""
    end = job.range_to
    if job.max_ids is not None:
        cap = job.range_from + job.max_ids - 1
        end = cap if end is None else min(end, cap)
    return IdRange(job.range_from, end)


# ------------------------------------------------------------------ создание


def _resolve_range(data: JobCreate) -> IdRange:
    try:
        if data.range is not None:
            if data.range_from is not None or data.range_to is not None:
                raise Invalid("Укажите либо range, либо range_from/range_to", "bad_range")
            return parse_range(data.range)
        return from_to(data.range_from, data.range_to)
    except RangeError as exc:
        raise Invalid(str(exc), "bad_range") from exc


def _validate_url_template(template: str, settings: Settings) -> str:
    if "{id}" not in template:
        raise Invalid("url_template должен содержать {id}", "bad_url_template")
    parts = urlsplit(template.replace("{id}", "0"))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise Invalid("url_template должен быть адресом http(s)", "bad_url_template")
    if parts.username or parts.password:
        raise Invalid("Учётные данные в url_template запрещены", "bad_url_template")
    if parts.hostname.lower() not in settings.allowed_hosts:
        raise Invalid(
            f"Хост {parts.hostname} не входит в список разрешённых целей обхода", "target_not_allowed"
        )
    return template


def create_job(
    session: Session, data: JobCreate, *, created_by: int | None = None, settings: Settings | None = None
) -> Job:
    settings = settings or get_settings()
    rng = _resolve_range(data)
    template = _validate_url_template(data.url_template or settings.default_url_template, settings)
    config: dict[str, Any] = {"url_template": template}
    if data.threads is not None:
        config["threads"] = data.threads

    job = Job(
        name=data.name,
        status=JobStatus.CREATED.value,
        attempt=1,
        range_from=rng.start,
        range_to=rng.end,
        chunk_size=data.chunk_size or settings.default_chunk_size,
        request_timeout_seconds=data.request_timeout_seconds,
        max_retries=data.max_retries,
        global_rate_limit_rps=data.global_rate_limit_rps,
        max_ids=data.max_ids,
        max_runtime_seconds=data.max_runtime_seconds,
        stop_on_consecutive_missing=data.stop_on_consecutive_missing,
        config=config,
        created_by=created_by,
    )
    session.add(job)
    session.flush()
    for item in data.workers:
        set_job_worker(
            session,
            job,
            item.worker_id,
            {"threads": item.threads, "rate_limit_rps": item.rate_limit_rps, "enabled": True},
        )
    log_event(session, "job_created", job_id=job.id, result="created", message=f"range={rng}")
    return job


def update_job(session: Session, job_id: int, changes: dict[str, Any]) -> Job:
    """Изменение настроек на лету (число потоков, лимиты) без остановки задания."""
    job = get_job(session, job_id, lock=True)
    if JobStatus(job.status) in TERMINAL_JOB_STATUSES:
        raise Conflict("Задание завершено, настройки менять нельзя", "job_finished")
    config = dict(job.config or {})
    for key, value in changes.items():
        if value is None and key not in NULLABLE_PATCH_FIELDS:
            raise Invalid(f"Поле {key} нельзя сбросить", "bad_value")
        if key == "threads":
            if value is None:
                config.pop("threads", None)
            else:
                config["threads"] = value
        else:
            setattr(job, key, value)
    job.config = config
    session.flush()
    log_event(session, "job_updated", job_id=job.id, result="ok", message=f"fields={sorted(changes)}")
    return job


def set_job_worker(session: Session, job: Job, worker_id: int, changes: dict[str, Any]) -> JobWorker:
    """Добавляет сервер в задание или меняет его индивидуальные настройки."""
    if session.get(Worker, worker_id) is None:
        raise NotFound(f"Сервер {worker_id} не найден", "worker_not_found")
    jw = session.get(JobWorker, (job.id, worker_id))
    if jw is None:
        jw = JobWorker(job_id=job.id, worker_id=worker_id, enabled=True)
        session.add(jw)
    for key in ("threads", "rate_limit_rps", "enabled"):
        if key in changes:
            setattr(jw, key, changes[key])
    session.flush()
    if changes.get("enabled") is False:
        release_leases(session, job_id=job.id, worker_id=worker_id)
    log_event(session, "job_worker_set", job_id=job.id, worker_id=worker_id, result="ok")
    return jw


# ------------------------------------------------------------------ настройки для агентов


def effective_threads(job: Job, jw: JobWorker | None, worker: Worker) -> int:
    """Приоритет: индивидуальное значение в задании > глобальное значение задания > значение сервера."""
    if jw is not None and jw.threads is not None:
        return jw.threads
    global_threads = (job.config or {}).get("threads")
    if global_threads:
        return int(global_threads)
    return worker.threads


def effective_rate_limit(session: Session, job: Job, jw: JobWorker | None) -> float | None:
    """Глобальный лимит делится поровну между активными серверами; индивидуальный не может его превысить."""
    rate = jw.rate_limit_rps if jw is not None else None
    if job.global_rate_limit_rps:
        online = session.scalar(
            select(func.count())
            .select_from(JobWorker)
            .join(Worker, Worker.id == JobWorker.worker_id)
            .where(
                JobWorker.job_id == job.id,
                JobWorker.enabled.is_(True),
                Worker.enabled.is_(True),
                Worker.status == WorkerStatus.ONLINE.value,
            )
        )
        share = job.global_rate_limit_rps / max(1, online or 0)
        rate = share if rate is None else min(rate, share)
    return rate


# ------------------------------------------------------------------ жизненный цикл


def transition(session: Session, job: Job, target: JobStatus, *, now: dt.datetime | None = None) -> None:
    now = now or utcnow()
    current = JobStatus(job.status)
    try:
        ensure_transition(current, target)
    except InvalidTransition as exc:
        raise Conflict(str(exc), "invalid_transition") from exc
    job.status = target.value
    if target == JobStatus.RUNNING and job.started_at is None:
        job.started_at = now
    if target in TERMINAL_JOB_STATUSES:
        job.finished_at = now
    session.flush()
    log_event(
        session, "job_status", job_id=job.id, result=target.value, message=f"{current.value} -> {target.value}"
    )


def _usable_workers(session: Session, job_id: int) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(JobWorker)
            .join(Worker, Worker.id == JobWorker.worker_id)
            .where(JobWorker.job_id == job_id, JobWorker.enabled.is_(True), Worker.enabled.is_(True))
        )
        or 0
    )


def start_job(session: Session, job_id: int, *, now: dt.datetime | None = None) -> Job:
    job = get_job(session, job_id, lock=True)
    if job.status == JobStatus.CREATED.value and not _usable_workers(session, job.id):
        raise Conflict("В задании нет доступных серверов", "no_workers")
    transition(session, job, JobStatus.VALIDATING, now=now)
    transition(session, job, JobStatus.RUNNING, now=now)
    return job


def pause_job(session: Session, job_id: int, *, now: dt.datetime | None = None) -> Job:
    """Мягкая пауза: новые блоки не выдаются, текущие завершаются."""
    job = get_job(session, job_id, lock=True)
    transition(session, job, JobStatus.PAUSING, now=now)
    settle_job(session, job, now=now)
    return job


def resume_job(session: Session, job_id: int, *, now: dt.datetime | None = None) -> Job:
    job = get_job(session, job_id, lock=True)
    if job.status == JobStatus.PAUSING.value:
        raise Conflict("Пауза ещё не завершена: дождитесь окончания текущих блоков", "pausing_in_progress")
    transition(session, job, JobStatus.RUNNING, now=now)
    return job


def stop(session: Session, job: Job, *, now: dt.datetime | None = None) -> None:
    """Немедленная остановка: блоки возвращаются в очередь с сохранением контрольных точек."""
    if job.status == JobStatus.CREATED.value:
        transition(session, job, JobStatus.STOPPED, now=now)
        return
    transition(session, job, JobStatus.STOPPING, now=now)
    release_leases(session, job_id=job.id)
    transition(session, job, JobStatus.STOPPED, now=now)


def stop_job(session: Session, job_id: int, *, now: dt.datetime | None = None) -> Job:
    job = get_job(session, job_id, lock=True)
    stop(session, job, now=now)
    return job


def _count_chunks(session: Session, job_id: int, *statuses: str) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(RangeChunk)
            .where(RangeChunk.job_id == job_id, RangeChunk.status.in_(statuses))
        )
        or 0
    )


def _is_exhausted(session: Session, job: Job) -> bool:
    """Конечный диапазон полностью нарезан на блоки и все блоки обработаны."""
    if _count_chunks(session, job.id, ChunkStatus.PENDING.value, ChunkStatus.LEASED.value):
        return False
    last_end = session.scalar(select(func.max(RangeChunk.range_end)).where(RangeChunk.job_id == job.id))
    if last_end is None:
        return False
    return next_chunk(effective_range(job), job.chunk_size, last_end) is None


def _consecutive_missing_reached(session: Session, job: Job) -> bool:
    """Условие остановки: от последнего найденного ID до границы сплошной обработки подряд нет материалов.

    Граница — конец последнего блока в цепочке полностью завершённых блоков с начала диапазона. «Найденным»
    считается любой результат, кроме not_found (в том числе ошибки: про них неизвестно, есть ли материал).
    """
    n = job.stop_on_consecutive_missing
    if not n:
        return False
    first_open = session.scalar(
        select(func.min(RangeChunk.range_start)).where(
            RangeChunk.job_id == job.id, RangeChunk.status != ChunkStatus.COMPLETED.value
        )
    )
    last_end = session.scalar(select(func.max(RangeChunk.range_end)).where(RangeChunk.job_id == job.id))
    if last_end is None:
        return False
    frontier = first_open - 1 if first_open is not None else last_end
    base = job.range_from - 1
    present = session.scalar(
        select(func.max(Item.external_id)).where(
            Item.job_id == job.id, Item.status != ItemStatus.NOT_FOUND.value, Item.external_id <= frontier
        )
    )
    return frontier - max(present if present is not None else base, base) >= n


def settle_job(session: Session, job: Job, *, now: dt.datetime | None = None) -> None:
    """Завершает промежуточные состояния: pausing -> paused, running -> completed."""
    status = JobStatus(job.status)
    if status == JobStatus.PAUSING:
        if not _count_chunks(session, job.id, ChunkStatus.LEASED.value):
            transition(session, job, JobStatus.PAUSED, now=now)
    elif status == JobStatus.RUNNING:
        if _consecutive_missing_reached(session, job):
            release_leases(session, job_id=job.id)  # агенты получат lease_lost и бросят блок
            log_event(session, "job_stop_condition", job_id=job.id, result="consecutive_missing",
                      message=f"подряд отсутствует не менее {job.stop_on_consecutive_missing} ID")
            transition(session, job, JobStatus.COMPLETED, now=now)
            return
        if _is_exhausted(session, job):
            failed = _count_chunks(session, job.id, ChunkStatus.FAILED.value)
            if failed:
                log_event(
                    session,
                    "job_finished_with_failed_chunks",
                    level="WARN",
                    job_id=job.id,
                    result="completed",
                    message=f"failed_chunks={failed}",
                )
            transition(session, job, JobStatus.COMPLETED, now=now)


# ------------------------------------------------------------------ чтение


def list_jobs(session: Session, *, status: str | None, limit: int, offset: int) -> list[Job]:
    stmt = select(Job)
    if status:
        stmt = stmt.where(Job.status == status)
    return list(session.scalars(stmt.order_by(Job.id.desc()).limit(limit).offset(offset)).all())


def job_stats(session: Session, job: Job) -> dict[str, Any]:
    eff = effective_range(job)
    chunk_rows = session.execute(
        select(
            RangeChunk.status,
            func.count(),
            func.coalesce(func.sum(RangeChunk.next_id - RangeChunk.range_start), 0),
        )
        .where(RangeChunk.job_id == job.id)
        .group_by(RangeChunk.status)
    ).all()
    chunks = {status: int(count) for status, count, _ in chunk_rows}
    processed = sum(int(done) for _, _, done in chunk_rows)

    item_rows = session.execute(
        select(Item.status, func.count()).where(Item.job_id == job.id).group_by(Item.status)
    ).all()
    total = eff.size()
    return {
        "effective_range": str(eff),
        "total_ids": total,
        "processed_ids": processed,
        "progress": (processed / total) if total else None,
        "chunks": chunks,
        "items": {status: int(count) for status, count in item_rows},
    }


def job_workers_view(session: Session, job: Job) -> list[dict[str, Any]]:
    rows = session.execute(
        select(JobWorker, Worker)
        .join(Worker, Worker.id == JobWorker.worker_id)
        .where(JobWorker.job_id == job.id)
        .order_by(Worker.name)
    ).all()
    return [
        {
            "worker_id": worker.id,
            "name": worker.name,
            "enabled": jw.enabled,
            "worker_status": worker.status,
            "threads": effective_threads(job, jw, worker),
            "rate_limit_rps": effective_rate_limit(session, job, jw),
        }
        for jw, worker in rows
    ]


def job_detail(session: Session, job: Job) -> dict[str, Any]:
    data = JobOut.model_validate(job).model_dump()
    data["stats"] = job_stats(session, job)
    data["workers"] = job_workers_view(session, job)
    return data


def list_items(
    session: Session, job_id: int, *, status: str | None, limit: int, offset: int
) -> tuple[int, list[Item]]:
    get_job(session, job_id)
    base = select(Item).where(Item.job_id == job_id)
    if status:
        base = base.where(Item.status == status)
    total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = session.scalars(base.order_by(Item.external_id).limit(limit).offset(offset)).all()
    return total, list(rows)


def list_chunks(session: Session, job_id: int, *, limit: int, offset: int) -> list[RangeChunk]:
    get_job(session, job_id)
    stmt = (
        select(RangeChunk)
        .where(RangeChunk.job_id == job_id)
        .order_by(RangeChunk.range_start)
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt).all())


def list_events(session: Session, job_id: int, *, level: str | None, limit: int) -> list[Event]:
    get_job(session, job_id)
    stmt = select(Event).where(Event.job_id == job_id)
    if level:
        stmt = stmt.where(Event.level == level.upper())
    return list(session.scalars(stmt.order_by(Event.id.desc()).limit(limit)).all())


def list_files(session: Session, job_id: int, *, limit: int, offset: int) -> tuple[int, list[dict[str, Any]]]:
    """Скачанные файлы задания вместе с названием материала."""
    from app.models import Download, FileRecord, MediaMetadata

    get_job(session, job_id)
    base = (
        select(FileRecord, Item, Download, MediaMetadata)
        .join(Download, Download.id == FileRecord.download_id)
        .join(Item, Item.id == Download.item_id)
        .outerjoin(MediaMetadata, MediaMetadata.item_id == Item.id)
        .where(Item.job_id == job_id)
    )
    total = (
        session.scalar(
            select(func.count())
            .select_from(FileRecord)
            .join(Download, Download.id == FileRecord.download_id)
            .join(Item, Item.id == Download.item_id)
            .where(Item.job_id == job_id)
        )
        or 0
    )
    rows = session.execute(base.order_by(Item.external_id).limit(limit).offset(offset)).all()
    return total, [
        {
            "external_id": item.external_id,
            "title": media.title if media else None,
            "mime_type": f.mime_type,
            "size_bytes": f.size_bytes,
            "checksum_sha256": f.checksum_sha256,
            "storage_path": f.storage_path,
            "worker_id": d.worker_id,
            "completed_at": f.completed_at,
        }
        for f, item, d, media in rows
    ]


RETRYABLE_ITEM_STATUSES = (ItemStatus.FAILED.value, ItemStatus.RATE_LIMITED.value, ItemStatus.RETRY_WAIT.value)
RETRY_FROM_STATUSES = (JobStatus.COMPLETED.value, JobStatus.STOPPED.value, JobStatus.FAILED.value, JobStatus.PAUSED.value)


def retry_failed(session: Session, job_id: int, *, created_by: int | None = None, start: bool = True) -> Job:
    """Новая попытка для ошибочных элементов: создаёт задание на диапазон от первого до последнего ошибочного ID.

    Завершённые задания не возобновляются (конечные состояния), поэтому повтор — это новое задание с теми же
    настройками и серверами (attempt + 1). Уже скачанные в этом диапазоне файлы агент распознаёт и не качает заново.
    """
    job = get_job(session, job_id)
    if job.status not in RETRY_FROM_STATUSES:
        raise Conflict("Повторить можно завершённое, остановленное или приостановленное задание", "job_not_finished")
    lo, hi, count = session.execute(
        select(func.min(Item.external_id), func.max(Item.external_id), func.count()).where(
            Item.job_id == job.id, Item.status.in_(RETRYABLE_ITEM_STATUSES)
        )
    ).one()
    if not count:
        raise Conflict("В задании нет ошибочных элементов", "nothing_to_retry")
    config = dict(job.config or {})
    config["retry_of"] = job.id
    new = Job(
        name=f"{job.name} (повтор {job.attempt + 1})"[:128],
        status=JobStatus.CREATED.value,
        attempt=job.attempt + 1,
        range_from=lo,
        range_to=hi,
        chunk_size=job.chunk_size,
        request_timeout_seconds=job.request_timeout_seconds,
        max_retries=job.max_retries,
        global_rate_limit_rps=job.global_rate_limit_rps,
        config=config,
        created_by=created_by,
    )
    session.add(new)
    session.flush()
    for jw in session.scalars(select(JobWorker).where(JobWorker.job_id == job.id, JobWorker.enabled.is_(True))):
        set_job_worker(session, new, jw.worker_id, {"threads": jw.threads, "rate_limit_rps": jw.rate_limit_rps, "enabled": True})
    log_event(session, "job_retry", job_id=new.id, result="created", message=f"retry_of={job.id}; failed_items={count}; range={lo}-{hi}")
    if start:
        start_job(session, new.id)
    return new
