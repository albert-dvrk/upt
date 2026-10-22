from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import OperatorDep, SessionDep, ViewerDep
from app.schemas import (
    ChunkOut,
    EventOut,
    FilePage,
    ItemPage,
    JobCreate,
    JobDetail,
    JobOut,
    JobPatch,
    JobWorkerOut,
    JobWorkerPatch,
)
from app.services import auth as auth_svc
from app.services import export as export_svc
from app.services import jobs as jobs_svc

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("", response_model=JobOut, status_code=201)
def create_job(data: JobCreate, session: SessionDep, who: OperatorDep):
    job = jobs_svc.create_job(session, data)
    auth_svc.audit(session, who, "job_create", "job", job.id, ip=who.ip, details={"name": job.name})
    session.commit()
    return job


@router.get("", response_model=list[JobOut])
def list_jobs(
    session: SessionDep, _: ViewerDep,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return jobs_svc.list_jobs(session, status=status, limit=limit, offset=offset)


@router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: int, session: SessionDep, _: ViewerDep):
    job = jobs_svc.get_job(session, job_id)
    return jobs_svc.job_detail(session, job)


@router.patch("/{job_id}", response_model=JobOut)
def patch_job(job_id: int, data: JobPatch, session: SessionDep, who: OperatorDep):
    changes = data.model_dump(exclude_unset=True)
    job = jobs_svc.update_job(session, job_id, changes)
    auth_svc.audit(session, who, "job_update", "job", job_id, ip=who.ip, details=changes)
    session.commit()
    return job


@router.post("/{job_id}/start", response_model=JobOut)
def start_job(job_id: int, session: SessionDep, who: OperatorDep):
    job = jobs_svc.start_job(session, job_id)
    auth_svc.audit(session, who, "job_start", "job", job_id, ip=who.ip)
    session.commit()
    return job


@router.post("/{job_id}/pause", response_model=JobOut)
def pause_job(job_id: int, session: SessionDep, who: OperatorDep):
    job = jobs_svc.pause_job(session, job_id)
    auth_svc.audit(session, who, "job_pause", "job", job_id, ip=who.ip)
    session.commit()
    return job


@router.post("/{job_id}/resume", response_model=JobOut)
def resume_job(job_id: int, session: SessionDep, who: OperatorDep):
    job = jobs_svc.resume_job(session, job_id)
    auth_svc.audit(session, who, "job_resume", "job", job_id, ip=who.ip)
    session.commit()
    return job


@router.post("/{job_id}/stop", response_model=JobOut)
def stop_job(job_id: int, session: SessionDep, who: OperatorDep):
    job = jobs_svc.stop_job(session, job_id)
    auth_svc.audit(session, who, "job_stop", "job", job_id, ip=who.ip)
    session.commit()
    return job


@router.put("/{job_id}/workers/{worker_id}", response_model=JobWorkerOut)
def put_job_worker(job_id: int, worker_id: int, data: JobWorkerPatch, session: SessionDep, who: OperatorDep):
    """Добавить сервер в задание или изменить его потоки, лимит, включённость."""
    job = jobs_svc.get_job(session, job_id, lock=True)
    changes = data.model_dump(exclude_unset=True)
    jobs_svc.set_job_worker(session, job, worker_id, changes)
    auth_svc.audit(session, who, "job_worker_set", "job", job_id, ip=who.ip, details={"worker_id": worker_id, **changes})
    session.commit()
    view = jobs_svc.job_workers_view(session, job)
    return next(item for item in view if item["worker_id"] == worker_id)


@router.get("/{job_id}/items", response_model=ItemPage)
def list_items(
    job_id: int,
    session: SessionDep, _: ViewerDep,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    total, items = jobs_svc.list_items(session, job_id, status=status, limit=limit, offset=offset)
    return {"total": total, "items": items}


@router.get("/{job_id}/files", response_model=FilePage)
def list_files(
    job_id: int,
    session: SessionDep, _: ViewerDep,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    total, files = jobs_svc.list_files(session, job_id, limit=limit, offset=offset)
    return {"total": total, "files": files}


@router.get("/{job_id}/chunks", response_model=list[ChunkOut])
def list_chunks(
    job_id: int,
    session: SessionDep, _: ViewerDep,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    return jobs_svc.list_chunks(session, job_id, limit=limit, offset=offset)


@router.get("/{job_id}/events", response_model=list[EventOut])
def list_events(
    job_id: int,
    session: SessionDep, _: ViewerDep,
    level: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
):
    return jobs_svc.list_events(session, job_id, level=level, limit=limit)


@router.post("/{job_id}/retry-failed", response_model=JobOut, status_code=201)
def retry_failed(job_id: int, session: SessionDep, who: OperatorDep):
    """Повтор ошибочных элементов: новое задание (попытка) на диапазон ошибок с теми же настройками."""
    job = jobs_svc.retry_failed(session, job_id, created_by=who.user_id or None)
    auth_svc.audit(session, who, "job_retry_failed", "job", job_id, ip=who.ip, details={"new_job": job.id})
    session.commit()
    return job


@router.get("/{job_id}/export")
def export_job(job_id: int, request: Request, session: SessionDep, who: ViewerDep,
               format: str = Query("csv", pattern="^(csv|json)$"), status: str | None = None):
    """Выгрузка результатов (CSV или JSON). Действие попадает в журнал аудита."""
    export_svc.check_job(session, job_id)
    auth_svc.audit(session, who, "job_export", "job", job_id, ip=who.ip, details={"format": format, "status": status})
    session.commit()
    media = "text/csv; charset=utf-8" if format == "csv" else "application/json"
    return StreamingResponse(
        export_svc.stream_export(request.app.state.session_factory, job_id, format, status),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="job-{job_id}.{format}"'},
    )
