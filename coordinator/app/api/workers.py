from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import SessionDep
from app.schemas import (
    CompleteRequest,
    CompleteResponse,
    FailRequest,
    FailResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    LeaseResponse,
    MetricOut,
    ProgressRequest,
    ProgressResponse,
    RegisterRequest,
    RegisterResponse,
    WorkerCreate,
    WorkerOut,
    WorkerPatch,
)
from app.services import leases as leases_svc
from app.services import workers as workers_svc

router = APIRouter(prefix="/api/workers", tags=["workers"])


# --- управление серверами (оператор / администратор)


@router.post("", response_model=WorkerOut, status_code=201)
def create_worker(data: WorkerCreate, session: SessionDep):
    worker = workers_svc.create_worker(session, data)
    session.commit()
    return worker


@router.get("", response_model=list[WorkerOut])
def list_workers(session: SessionDep):
    return workers_svc.list_workers(session)


# --- протокол агента. Маршрут /register объявлен раньше /{worker_id}.


@router.post("/register", response_model=RegisterResponse)
def register(data: RegisterRequest, session: SessionDep):
    result = workers_svc.register_agent(session, data.name, data.agent_version, data.resources)
    session.commit()
    return result


@router.get("/{worker_id}", response_model=WorkerOut)
def get_worker(worker_id: int, session: SessionDep):
    return workers_svc.get_worker(session, worker_id)


@router.patch("/{worker_id}", response_model=WorkerOut)
def patch_worker(worker_id: int, data: WorkerPatch, session: SessionDep):
    worker = workers_svc.update_worker(session, worker_id, data.model_dump(exclude_none=True))
    session.commit()
    return worker


@router.get("/{worker_id}/metrics", response_model=list[MetricOut])
def get_metrics(worker_id: int, session: SessionDep, limit: int = Query(100, ge=1, le=1000)):
    return workers_svc.list_metrics(session, worker_id, limit=limit)


@router.post("/{worker_id}/heartbeat", response_model=HeartbeatResponse)
def heartbeat(worker_id: int, session: SessionDep, data: HeartbeatRequest | None = None):
    data = data or HeartbeatRequest()
    result = workers_svc.heartbeat(
        session,
        worker_id,
        agent_version=data.agent_version,
        resources=data.resources,
        metrics=data.metrics,
    )
    session.commit()
    return result


@router.post("/{worker_id}/lease", response_model=LeaseResponse)
def lease(worker_id: int, session: SessionDep):
    result = leases_svc.lease_chunk(session, worker_id)
    session.commit()
    return result


@router.post("/{worker_id}/progress", response_model=ProgressResponse)
def progress(worker_id: int, data: ProgressRequest, session: SessionDep):
    result = leases_svc.report_progress(session, worker_id, data.chunk_id, data.next_id, data.items)
    session.commit()
    return result


@router.post("/{worker_id}/complete", response_model=CompleteResponse)
def complete(worker_id: int, data: CompleteRequest, session: SessionDep):
    result = leases_svc.complete_chunk(session, worker_id, data.chunk_id, data.items)
    session.commit()
    return result


@router.post("/{worker_id}/fail", response_model=FailResponse)
def fail(worker_id: int, data: FailRequest, session: SessionDep):
    result = leases_svc.fail_chunk(
        session, worker_id, data.chunk_id, data.error_code, data.message, data.retryable
    )
    session.commit()
    return result
