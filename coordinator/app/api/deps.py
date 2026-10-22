from __future__ import annotations

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, Worker
from app.security import has_role, token_matches, verify_session
from app.services import auth as auth_svc
from app.services.deploy import SshDeployer
from app.services.errors import Forbidden, Unauthorized

COOKIE = "vf_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def get_session(request: Request) -> Iterator[Session]:
    """Сессия на запрос. commit вызывает сам обработчик: ответ уходит только после фиксации."""
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def get_deployer(request: Request) -> SshDeployer:
    return request.app.state.deployer


DeployerDep = Annotated[SshDeployer, Depends(get_deployer)]


@dataclass
class Principal:
    user_id: int
    username: str
    roles: list[str]
    csrf: str
    ip: str | None = None


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def authenticate_user(request: Request, session: Session, *, check_csrf: bool = True) -> Principal:
    """Пользователь по подписанной cookie. Права и активность берутся из БД при каждом запросе."""
    settings = request.app.state.settings
    if not settings.auth_enabled:
        return Principal(0, "anonymous", ["administrator"], "", client_ip(request))
    data = verify_session(request.app.state.secret_key, request.cookies.get(COOKIE))
    if not data:
        raise Unauthorized("Требуется вход в систему")
    user = session.get(User, data["uid"])
    if user is None or not user.is_active:
        raise Unauthorized("Сессия недействительна")
    if check_csrf and request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not secrets.compare_digest(sent, data["csrf"]):
            raise Forbidden("Неверный CSRF-токен", "csrf_failed")
    return Principal(user.id, user.username, auth_svc.user_roles(session, user.id), data["csrf"], client_ip(request))


def require(role: str):
    def dependency(request: Request, session: SessionDep) -> Principal:
        principal = authenticate_user(request, session)
        if not has_role(principal.roles, role):
            raise Forbidden(f"Недостаточно прав: нужна роль «{role}»")
        return principal

    return dependency


ViewerDep = Annotated[Principal, Depends(require("viewer"))]
OperatorDep = Annotated[Principal, Depends(require("operator"))]
AdminDep = Annotated[Principal, Depends(require("administrator"))]


# ---------------------------------------------------------------- агенты


def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token else None


def check_agent_token(request: Request, worker: Worker | None) -> None:
    if not request.app.state.settings.auth_enabled:
        return
    if worker is None or not token_matches(bearer_token(request), worker.agent_token_hash):
        raise Unauthorized("Неверный или отсутствующий токен агента", "agent_unauthorized")


def agent_auth(worker_id: int, request: Request, session: SessionDep) -> None:
    """Токен агента должен принадлежать серверу из пути запроса."""
    check_agent_token(request, session.get(Worker, worker_id))


AgentDep = Depends(agent_auth)


def find_worker_by_name(session: Session, name: str) -> Worker | None:
    return session.scalar(select(Worker).where(Worker.name == name))
