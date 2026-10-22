"""Криптографические примитивы: хеши паролей, подписанные сессии, токены агентов.

Только стандартная библиотека. Пароли — scrypt с индивидуальной солью; сессионный токен —
HMAC-SHA256 над JSON с временем жизни; токены агентов хранятся в БД только в виде SHA-256.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

ROLE_LEVEL = {"viewer": 1, "operator": 2, "administrator": 3}
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------- пароли


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, maxmem=64 * 1024 * 1024, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = _unb64(digest)
        actual = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def check_password_policy(password: str) -> str | None:
    """Возвращает текст ошибки или None."""
    if len(password) < 10:
        return "Пароль должен содержать не менее 10 символов"
    if password.lower() == password or password.upper() == password or not any(c.isdigit() for c in password):
        return "Пароль должен содержать строчные и заглавные буквы и цифры"
    return None


# ---------------------------------------------------------------- сессии


def sign_session(secret: str, user_id: int, username: str, ttl: int, *, now: float | None = None) -> tuple[str, str]:
    """Возвращает (токен сессии, CSRF-токен). Права пользователя берутся из БД при каждом запросе."""
    now = now if now is not None else time.time()
    csrf = secrets.token_urlsafe(24)
    payload = json.dumps({"uid": user_id, "usr": username, "exp": int(now + ttl), "csrf": csrf}, separators=(",", ":")).encode()
    body = _b64(payload)
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}", csrf


def verify_session(secret: str, token: str | None, *, now: float | None = None) -> dict | None:
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_unb64(body))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("exp", 0) < (now if now is not None else time.time()):
        return None
    return data


# ---------------------------------------------------------------- токены агентов


def new_agent_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(token: str | None, stored_hash: str | None) -> bool:
    if not token or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(token), stored_hash)


def has_role(roles: list[str] | set[str], required: str) -> bool:
    need = ROLE_LEVEL[required]
    return any(ROLE_LEVEL.get(r, 0) >= need for r in roles)
