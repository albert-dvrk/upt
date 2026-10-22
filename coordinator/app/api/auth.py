from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.api.deps import COOKIE, SessionDep, authenticate_user, client_ip
from app.security import sign_session
from app.services import auth as auth_svc
from app.services.errors import Unauthorized

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _session_payload(username: str, roles: list[str], csrf: str) -> dict:
    return {"username": username, "roles": roles, "csrf_token": csrf}


@router.post("/login")
def login(data: LoginRequest, request: Request, response: Response, session: SessionDep):
    settings = request.app.state.settings
    ip = client_ip(request) or "?"
    throttle = request.app.state.login_throttle
    throttle.check(ip, data.username)
    user = auth_svc.authenticate(session, data.username, data.password)
    if user is None:
        throttle.failed(ip, data.username)
        auth_svc.audit(session, None, "login_failed", "user", data.username, ip=ip, username=data.username[:64])
        session.commit()
        raise Unauthorized("Неверное имя пользователя или пароль", "bad_credentials")
    throttle.succeeded(ip, data.username)
    token, csrf = sign_session(request.app.state.secret_key, user.id, user.username, settings.session_ttl_seconds)
    response.set_cookie(COOKIE, token, max_age=settings.session_ttl_seconds, httponly=True,
                        samesite="strict", secure=settings.cookie_secure, path="/")
    roles = auth_svc.user_roles(session, user.id)
    auth_svc.audit(session, None, "login", "user", user.username, ip=ip, username=user.username)
    session.commit()
    return _session_payload(user.username, roles, csrf)


@router.get("/me")
def me(request: Request, session: SessionDep):
    principal = authenticate_user(request, session)
    return _session_payload(principal.username, principal.roles, principal.csrf)


@router.post("/logout")
def logout(request: Request, response: Response, session: SessionDep):
    try:
        principal = authenticate_user(request, session)
        auth_svc.audit(session, principal, "logout", "user", principal.username, ip=principal.ip)
        session.commit()
    except Exception:  # noqa: BLE001 — выход всегда очищает cookie, даже если сессия уже недействительна
        pass
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}
