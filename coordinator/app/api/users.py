from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import AdminDep, SessionDep
from app.services import auth as auth_svc
from app.models import User
from sqlalchemy import select

router = APIRouter(tags=["users"])


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    roles: list[str] = Field(default_factory=lambda: ["viewer"])


class UserPatch(BaseModel):
    is_active: bool | None = None
    roles: list[str] | None = None
    password: str | None = Field(None, max_length=256)


@router.get("/api/users")
def list_users(session: SessionDep, _: AdminDep):
    return [auth_svc.user_view(session, u) for u in session.scalars(select(User).order_by(User.username))]


@router.post("/api/users", status_code=201)
def create_user(data: UserCreate, session: SessionDep, admin: AdminDep):
    user = auth_svc.create_user(session, data.username, data.password, data.roles)
    auth_svc.audit(session, admin, "user_create", "user", user.username, ip=admin.ip, details={"roles": sorted(data.roles)})
    session.commit()
    return auth_svc.user_view(session, user)


@router.patch("/api/users/{user_id}")
def patch_user(user_id: int, data: UserPatch, session: SessionDep, admin: AdminDep):
    user = auth_svc.update_user(session, user_id, is_active=data.is_active, roles=data.roles, password=data.password)
    changes = {k: v for k, v in {"is_active": data.is_active, "roles": data.roles,
                                 "password_changed": True if data.password else None}.items() if v is not None}
    auth_svc.audit(session, admin, "user_update", "user", user.username, ip=admin.ip, details=changes)
    session.commit()
    return auth_svc.user_view(session, user)


@router.get("/api/audit")
def list_audit(session: SessionDep, _: AdminDep, action: str | None = None, username: str | None = None,
               limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
    rows = auth_svc.list_audit(session, action=action, username=username, limit=limit, offset=offset)
    return [{"id": r.id, "ts": r.ts, "username": r.username, "action": r.action, "target_type": r.target_type,
             "target_id": r.target_id, "ip": r.ip, "details": r.details} for r in rows]
