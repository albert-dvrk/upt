from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas import (
    ChunkOut,
    EventOut,
    ItemPage,
    JobCreate,
    JobDetail,
    JobOut,
    JobPatch,
    JobWorkerOut,
    JobWorkerPatch,
)
from app.services import jobs as jobs_svc

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("", response_model=JobOut, status_code=201)
def create_job(data: JobCreate, session: SessionDep):
    job = jobs_svc.create_job(session, data)
    session.commit()
    return job


@router.get("", response_model=list[JobOut])
def list_jobs(
    session: SessionDep,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return jobs_svc.list_jobs(session, status=status, limit=limit, offset=offset)


@router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: int, session: SessionDep):
    job = jobs_svc.get_job(session, job_id)
    return jobs_svc.job_detail(session, job)


@router.patch("/{job_id}", response_model=JobOut)
def patch_job(job_id: int, data: JobPatch, session: SessionDep):
    job = jobs_svc.update_job(session, job_id, data.model_dump(exclude_unset=True))
    session.commit()
    return job


@router.post("/{job_id}/start", response_model=JobOut)
def start_job(job_id: int, session: SessionDep):
    job = jobs_svc.start_job(session, job_id)
    session.commit()
    return job


@router.post("/{job_id}/pause", response_model=JobOut)
def pause_job(job_id: int, session: SessionDep):
    job = jobs_svc.pause_job(session, job_id)
    session.commit()
    return job


@router.post("/{job_id}/resume", response_model=JobOut)
def resume_job(job_id: int, session: SessionDep):
    job = jobs_svc.resume_job(session, job_id)
    session.commit()
    return job


@router.post("/{job_id}/stop", response_model=JobOut)
def stop_job(job_id: int, session: SessionDep):
    job = jobs_svc.stop_job(session, job_id)
    session.commit()
    return job


@router.put("/{job_id}/workers/{worker_id}", response_model=JobWorkerOut)
def put_job_worker(job_id: int, worker_id: int, data: JobWorkerPatch, session: SessionDep):
    """Добавить сервер в задание или изменить его потоки, лимит, включённость."""
    job = jobs_svc.get_job(session, job_id, lock=True)
    jobs_svc.set_job_worker(session, job, worker_id, data.model_dump(exclude_unset=True))
    session.commit()
    view = jobs_svc.job_workers_view(session, job)
    return next(item for item in view if item["worker_id"] == worker_id)


@router.get("/{job_id}/items", response_model=ItemPage)
def list_items(
    job_id: int,
    session: SessionDep,
    status: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    total, items = jobs_svc.list_items(session, job_id, status=status, limit=limit, offset=offset)
    return {"total": total, "items": items}


@router.get("/{job_id}/chunks", response_model=list[ChunkOut])
def list_chunks(
    job_id: int,
    session: SessionDep,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    return jobs_svc.list_chunks(session, job_id, limit=limit, offset=offset)


@router.get("/{job_id}/events", response_model=list[EventOut])
def list_events(
    job_id: int,
    session: SessionDep,
    level: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
):
    return jobs_svc.list_events(session, job_id, level=level, limit=limit)
