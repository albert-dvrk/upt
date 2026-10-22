"""Предупреждения оператору: вычисляются по текущему состоянию БД при каждом запросе (без отдельного хранилища)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Item, Job, JobWorker, RangeChunk, Worker
from app.statuses import ChunkStatus, ItemStatus, JobStatus, WorkerStatus
from app.timeutil import as_utc, utcnow

STALL_SECONDS = 120        # у работающего задания нет новых результатов
WINDOW_SECONDS = 300       # окно оценки доли ошибок
ERROR_RATIO = 0.2          # доля ошибочных элементов в окне
MIN_SAMPLE = 10            # меньше элементов — выводов не делаем
GIB = 1024 ** 3
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _alert(severity: str, code: str, message: str, **ref: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "message": message, **ref}


def compute_alerts(session: Session, *, now: dt.datetime | None = None) -> list[dict[str, Any]]:
    now = now or utcnow()
    alerts: list[dict[str, Any]] = []
    running = list(session.scalars(select(Job).where(Job.status == JobStatus.RUNNING.value)))
    busy_workers = set(
        session.scalars(
            select(JobWorker.worker_id).where(
                JobWorker.enabled.is_(True), JobWorker.job_id.in_([j.id for j in running] or [0])
            )
        )
    )

    for w in session.scalars(select(Worker).where(Worker.enabled.is_(True))):
        beat = as_utc(w.last_heartbeat_at)
        if w.status == WorkerStatus.OFFLINE.value:
            ago = f"{int((now - beat).total_seconds() // 60)} мин назад" if beat else "никогда"
            sev = "critical" if w.id in busy_workers else "warning"
            alerts.append(_alert(sev, "worker_offline", f"Сервер {w.name} не отвечает (последний heartbeat: {ago})", worker_id=w.id))
            continue
        res = w.resources or {}
        free, limit = res.get("disk_free_bytes"), (w.min_free_space_gb or 0) * GIB
        if free is not None and limit:
            if free < limit:
                alerts.append(_alert("critical", "disk_low", f"На сервере {w.name} мало места: {free / GIB:.1f} ГБ при минимуме {limit / GIB:.0f} ГБ", worker_id=w.id))
            elif free < limit * 1.5:
                alerts.append(_alert("warning", "disk_low_soon", f"На сервере {w.name} место заканчивается: {free / GIB:.1f} ГБ", worker_id=w.id))
        scale = (res.get("runtime") or {}).get("throttle_scale")
        if isinstance(scale, (int, float)) and scale < 1:
            alerts.append(_alert("info", "throttled", f"Сервер {w.name}: нагрузка снижена до {scale:.0%} из-за ответов 429/5xx", worker_id=w.id))

    window = now - dt.timedelta(seconds=WINDOW_SECONDS)
    for job in running:
        last = session.scalar(select(func.max(Item.updated_at)).where(Item.job_id == job.id))
        last = as_utc(last) or as_utc(job.started_at)
        leased = session.scalar(
            select(func.count()).select_from(RangeChunk).where(RangeChunk.job_id == job.id, RangeChunk.status == ChunkStatus.LEASED.value)
        )
        if last and leased and (now - last).total_seconds() > STALL_SECONDS:
            alerts.append(_alert("warning", "job_stalled", f"Задание #{job.id} «{job.name}» не продвигается уже {int((now - last).total_seconds() // 60)} мин", job_id=job.id))
        rows = dict(session.execute(
            select(Item.status, func.count()).where(Item.job_id == job.id, Item.updated_at >= window).group_by(Item.status)
        ).all())
        total = sum(rows.values())
        bad = rows.get(ItemStatus.FAILED.value, 0)
        if total >= MIN_SAMPLE and bad / total >= ERROR_RATIO:
            alerts.append(_alert("warning", "high_error_rate", f"Задание #{job.id}: {bad} из {total} элементов за 5 мин завершились ошибкой", job_id=job.id))
        if rows.get(ItemStatus.RATE_LIMITED.value):
            alerts.append(_alert("warning", "rate_limited", f"Задание #{job.id}: целевой сервис ограничивает частоту запросов (429)", job_id=job.id))

    failed_chunks = session.scalars(select(RangeChunk).where(RangeChunk.status == ChunkStatus.FAILED.value).limit(20)).all()
    for c in failed_chunks:
        alerts.append(_alert("critical", "chunk_failed", f"Блок {c.range_start}–{c.range_end} задания #{c.job_id} не удалось обработать", job_id=c.job_id))
    retried = session.scalars(
        select(RangeChunk).where(RangeChunk.status != ChunkStatus.COMPLETED.value, RangeChunk.attempt >= 3,
                                 RangeChunk.status != ChunkStatus.FAILED.value).limit(20)
    ).all()
    for c in retried:
        alerts.append(_alert("warning", "chunk_retries", f"Блок {c.range_start}–{c.range_end} задания #{c.job_id} выдаётся уже в {c.attempt}-й раз", job_id=c.job_id))

    alerts.sort(key=lambda a: SEVERITY_ORDER[a["severity"]])
    return alerts


def active_downloads(session: Session, *, now: dt.datetime | None = None, max_age: float = 30.0) -> list[dict[str, Any]]:
    """Загрузки, идущие прямо сейчас: данные приходят в heartbeat агентов (resources.runtime.downloads)."""
    now = now or utcnow()
    out: list[dict[str, Any]] = []
    for w in session.scalars(select(Worker).where(Worker.status == WorkerStatus.ONLINE.value).order_by(Worker.name)):
        beat = as_utc(w.last_heartbeat_at)
        if beat is None or (now - beat).total_seconds() > max_age:
            continue
        for d in ((w.resources or {}).get("runtime") or {}).get("downloads", []):
            total, done = d.get("total"), d.get("bytes") or 0
            out.append({
                "worker_id": w.id, "worker": w.name, "job_id": d.get("job_id"), "external_id": d.get("external_id"),
                "bytes": done, "total": total, "bps": d.get("bps"),
                "percent": round(min(100.0, done / total * 100), 1) if total else None,
            })
    return out
