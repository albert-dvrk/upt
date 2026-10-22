from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.api.deps import (AdminDep, AgentDep, DeployerDep, OperatorDep, SessionDep, ViewerDep,
                          check_agent_token, find_worker_by_name)
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
from app.security import new_agent_token, hash_token
from app.services import auth as auth_svc
from app.services import leases as leases_svc
from app.services.deploy import Target
from app.services.events import log_event
from app.services.errors import ServiceError, Unauthorized
from app.services import workers as workers_svc

router = APIRouter(prefix="/api/workers", tags=["workers"])


# --- управление серверами (оператор / администратор)


@router.post("", response_model=WorkerOut, status_code=201)
def create_worker(data: WorkerCreate, session: SessionDep, who: AdminDep):
    worker = workers_svc.create_worker(session, data)
    auth_svc.audit(session, who, "worker_create", "worker", worker.id, ip=who.ip, details={"name": worker.name, "host": worker.host})
    session.commit()
    return worker


@router.get("", response_model=list[WorkerOut])
def list_workers(session: SessionDep, _: ViewerDep):
    return workers_svc.list_workers(session)


# --- протокол агента. Маршрут /register объявлен раньше /{worker_id}.


@router.post("/register", response_model=RegisterResponse)
def register(data: RegisterRequest, request: Request, session: SessionDep):
    worker = find_worker_by_name(session, data.name)
    if worker is None and request.app.state.settings.auth_enabled:
        raise Unauthorized("Неверный или отсутствующий токен агента", "agent_unauthorized")
    check_agent_token(request, worker)
    result = workers_svc.register_agent(session, data.name, data.agent_version, data.resources)
    session.commit()
    return result


@router.get("/{worker_id}", response_model=WorkerOut)
def get_worker(worker_id: int, session: SessionDep, _: ViewerDep):
    return workers_svc.get_worker(session, worker_id)


@router.patch("/{worker_id}", response_model=WorkerOut)
def patch_worker(worker_id: int, data: WorkerPatch, session: SessionDep, who: AdminDep):
    changes = data.model_dump(exclude_none=True)
    worker = workers_svc.update_worker(session, worker_id, changes)
    auth_svc.audit(session, who, "worker_update", "worker", worker_id, ip=who.ip, details={k: v for k, v in changes.items() if k != "ssh_key_ref"})
    session.commit()
    return worker


# --- управление агентом по SSH. Обработчики синхронные: FastAPI выполняет их в пуле потоков.


def _target(worker) -> Target:
    return Target(worker.host, worker.ssh_port, worker.ssh_user, worker.ssh_key_ref, worker.name)


def _remote(session, worker, deployer, operation: str, action, who=None):
    """Выполняет операцию на сервере и пишет событие (успех или ошибку), не раскрывая ключ."""
    try:
        result = action(deployer, _target(worker))
    except ServiceError as exc:
        log_event(session, operation, level="ERROR", component="deploy", worker_id=worker.id,
                  result="failed", error_code=exc.code, message=exc.message)
        if who is not None:
            auth_svc.audit(session, who, operation, "worker", worker.id, ip=who.ip, details={"result": "failed", "error": exc.code})
        session.commit()
        raise
    log_event(session, operation, component="deploy", worker_id=worker.id, result="ok")
    if who is not None and operation != "agent_status":
        auth_svc.audit(session, who, operation, "worker", worker.id, ip=who.ip, details={"result": "ok"})
    session.commit()
    return result


@router.post("/{worker_id}/agent/check")
def agent_check(worker_id: int, session: SessionDep, deployer: DeployerDep, who: AdminDep):
    worker = workers_svc.get_worker(session, worker_id)
    return _remote(session, worker, deployer, "ssh_check", lambda d, t: d.check(t), who)


@router.post("/{worker_id}/agent/deploy")
def agent_deploy(worker_id: int, session: SessionDep, deployer: DeployerDep, who: AdminDep):
    worker = workers_svc.get_worker(session, worker_id)
    return _remote(session, worker, deployer, "agent_deploy", lambda d, t: d.deploy(t), who)


@router.post("/{worker_id}/agent/start")
def agent_start(worker_id: int, request: Request, session: SessionDep, deployer: DeployerDep, who: AdminDep):
    """Выпускает новый токен агента (старый перестаёт действовать), сохраняет его хеш и запускает агента.

    Хеш фиксируется в БД до удаления по SSH: иначе агент, стартовав, получил бы отказ.
    Сам токен хранится только в файле agent.token на сервере (права 0600).
    """
    worker = workers_svc.get_worker(session, worker_id)
    settings = request.app.state.settings
    token, previous = None, worker.agent_token_hash
    if settings.auth_enabled:
        token = new_agent_token()
        worker.agent_token_hash = hash_token(token)
        session.commit()
    ca_pem = request.app.state.agent_ca_pem
    try:
        return _remote(session, worker, deployer, "agent_start",
                       lambda d, t: d.start(t, token=token, ca_pem=ca_pem), who)
    except ServiceError as exc:
        if token and exc.code != "agent_crashed":
            # старый агент мог остаться работать: возвращаем его токен, чтобы он не потерял доступ
            worker.agent_token_hash = previous
            session.commit()
        raise


@router.post("/{worker_id}/agent/stop")
def agent_stop(worker_id: int, session: SessionDep, deployer: DeployerDep, who: AdminDep):
    worker = workers_svc.get_worker(session, worker_id)
    return _remote(session, worker, deployer, "agent_stop", lambda d, t: d.stop(t), who)


@router.get("/{worker_id}/agent")
def agent_status(worker_id: int, session: SessionDep, deployer: DeployerDep, _: AdminDep):
    worker = workers_svc.get_worker(session, worker_id)
    return _remote(session, worker, deployer, "agent_status", lambda d, t: d.status(t))


@router.get("/{worker_id}/metrics", response_model=list[MetricOut])
def get_metrics(worker_id: int, session: SessionDep, _: ViewerDep, limit: int = Query(100, ge=1, le=1000)):
    return workers_svc.list_metrics(session, worker_id, limit=limit)


@router.post("/{worker_id}/heartbeat", response_model=HeartbeatResponse, dependencies=[AgentDep])
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


@router.post("/{worker_id}/lease", response_model=LeaseResponse, dependencies=[AgentDep])
def lease(worker_id: int, session: SessionDep):
    result = leases_svc.lease_chunk(session, worker_id)
    session.commit()
    return result


@router.post("/{worker_id}/progress", response_model=ProgressResponse, dependencies=[AgentDep])
def progress(worker_id: int, data: ProgressRequest, session: SessionDep):
    result = leases_svc.report_progress(session, worker_id, data.chunk_id, data.next_id, data.items)
    session.commit()
    return result


@router.post("/{worker_id}/complete", response_model=CompleteResponse, dependencies=[AgentDep])
def complete(worker_id: int, data: CompleteRequest, session: SessionDep):
    result = leases_svc.complete_chunk(session, worker_id, data.chunk_id, data.items)
    session.commit()
    return result


@router.post("/{worker_id}/fail", response_model=FailResponse, dependencies=[AgentDep])
def fail(worker_id: int, data: FailRequest, session: SessionDep):
    result = leases_svc.fail_chunk(
        session, worker_id, data.chunk_id, data.error_code, data.message, data.retryable
    )
    session.commit()
    return result


@router.post("/{worker_id}/token/revoke")
def revoke_token(worker_id: int, session: SessionDep, who: AdminDep):
    """Отзывает токен агента: агент перестаёт проходить проверку до следующего «Запустить»."""
    worker = workers_svc.get_worker(session, worker_id)
    worker.agent_token_hash = None
    auth_svc.audit(session, who, "agent_token_revoke", "worker", worker_id, ip=who.ip)
    session.commit()
    return {"revoked": True}
