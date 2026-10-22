"""Пользователи, роли, вход и журнал аудита."""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AuditLog, Role, User, UserRole
from app.security import ROLE_LEVEL, check_password_policy, hash_password, verify_password
from app.services.errors import Conflict, Invalid, NotFound, TooManyRequests

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def ensure_roles(session: Session) -> dict[str, Role]:
    existing = {r.name: r for r in session.scalars(select(Role))}
    for name in ROLE_LEVEL:
        if name not in existing:
            role = Role(name=name)
            session.add(role)
            existing[name] = role
    session.flush()
    return existing


def user_roles(session: Session, user_id: int) -> list[str]:
    rows = session.scalars(
        select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == user_id)
    )
    return sorted(rows, key=lambda r: -ROLE_LEVEL.get(r, 0))


def user_view(session: Session, user: User) -> dict[str, Any]:
    return {"id": user.id, "username": user.username, "is_active": user.is_active,
            "roles": user_roles(session, user.id), "created_at": user.created_at}


def _set_roles(session: Session, user: User, roles: list[str]) -> None:
    unknown = [r for r in roles if r not in ROLE_LEVEL]
    if unknown:
        raise Invalid(f"Неизвестные роли: {', '.join(unknown)}", "bad_role")
    if not roles:
        raise Invalid("У пользователя должна быть хотя бы одна роль", "no_role")
    catalog = ensure_roles(session)
    session.query(UserRole).filter(UserRole.user_id == user.id).delete()
    for name in set(roles):
        session.add(UserRole(user_id=user.id, role_id=catalog[name].id))
    session.flush()


def create_user(session: Session, username: str, password: str, roles: list[str]) -> User:
    if not USERNAME_RE.match(username):
        raise Invalid("Имя пользователя: 3–64 символа (буквы, цифры, . _ -)", "bad_username")
    problem = check_password_policy(password)
    if problem:
        raise Invalid(problem, "weak_password")
    if session.scalar(select(User).where(func.lower(User.username) == username.lower())):
        raise Conflict("Пользователь с таким именем уже существует", "user_exists")
    user = User(username=username, password_hash=hash_password(password), is_active=True)
    session.add(user)
    session.flush()
    _set_roles(session, user, roles)
    return user


def _active_admins(session: Session) -> int:
    return session.scalar(
        select(func.count(func.distinct(User.id)))
        .join(UserRole, UserRole.user_id == User.id)
        .join(Role, Role.id == UserRole.role_id)
        .where(User.is_active.is_(True), Role.name == "administrator")
    ) or 0


def update_user(session: Session, user_id: int, *, is_active: bool | None = None,
                roles: list[str] | None = None, password: str | None = None) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise NotFound("Пользователь не найден", "user_not_found")
    was_admin = user.is_active and "administrator" in user_roles(session, user.id)
    if password is not None:
        problem = check_password_policy(password)
        if problem:
            raise Invalid(problem, "weak_password")
        user.password_hash = hash_password(password)
    if is_active is not None:
        user.is_active = is_active
    if roles is not None:
        _set_roles(session, user, roles)
    still_admin = user.is_active and "administrator" in user_roles(session, user.id)
    if was_admin and not still_admin and _active_admins(session) == 0:
        session.rollback()
        raise Conflict("Нельзя отключить или понизить последнего администратора", "last_admin")
    return user


def bootstrap_admin(session: Session, settings: Settings) -> bool:
    """Создаёт первого администратора из настроек, если пользователей ещё нет."""
    if not settings.admin_password or session.scalar(select(func.count()).select_from(User)):
        return False
    create_user(session, settings.admin_username, settings.admin_password, ["administrator"])
    audit(session, None, "bootstrap_admin", "user", settings.admin_username)
    return True


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.scalar(select(User).where(func.lower(User.username) == username.lower()))
    if user is None:
        verify_password(password, _DUMMY_HASH)  # выравниваем время ответа: имя не угадывается по задержке
        return None
    if not verify_password(password, user.password_hash) or not user.is_active:
        return None
    return user


class LoginThrottle:
    """Защита от перебора: после N неудач для пары (адрес, имя) вход блокируется на время."""

    def __init__(self, max_failures: int, lock_seconds: int, clock=time.monotonic) -> None:
        self.max, self.lock, self.clock = max_failures, lock_seconds, clock
        self._state: dict[tuple[str, str], tuple[int, float]] = {}
        self._mutex = threading.Lock()

    def check(self, ip: str, username: str) -> None:
        with self._mutex:
            failures, until = self._state.get((ip, username.lower()), (0, 0.0))
            if failures >= self.max and self.clock() < until:
                raise TooManyRequests("Слишком много неудачных попыток входа, повторите позже")

    def failed(self, ip: str, username: str) -> None:
        key = (ip, username.lower())
        with self._mutex:
            failures, until = self._state.get(key, (0, 0.0))
            if failures >= self.max and self.clock() >= until:
                failures = 0  # блокировка истекла
            self._state[key] = (failures + 1, self.clock() + self.lock)

    def succeeded(self, ip: str, username: str) -> None:
        with self._mutex:
            self._state.pop((ip, username.lower()), None)


def audit(session: Session, principal, action: str, target_type: str | None = None,
          target_id: Any = None, *, ip: str | None = None, details: dict | None = None,
          username: str | None = None) -> None:
    """Запись в журнал аудита. Секреты (пароли, токены, ключи) в details передавать нельзя."""
    session.add(AuditLog(
        user_id=getattr(principal, "user_id", None) or None,
        username=username or getattr(principal, "username", None),
        action=action, target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        ip=ip, details=details,
    ))


def list_audit(session: Session, *, action: str | None, username: str | None, limit: int, offset: int):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if username:
        stmt = stmt.where(AuditLog.username == username)
    return list(session.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit).offset(offset)))
