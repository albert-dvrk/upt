"""Общий клиент для скриптов стенда: HTTPS с проверкой сертификата, вход, cookie и CSRF-заголовок.

Настройка через окружение (значения по умолчанию подходят для стенда из deploy/):
  VF_API       адрес координатора                   https://localhost:8001
  VF_CA        сертификат для проверки HTTPS        deploy/secrets/tls/server.crt
  VF_USER      имя пользователя                     admin
  VF_PASSWORD  пароль (иначе VF_ADMIN_PASSWORD из deploy/.env)
Запросы не к координатору (например, к mock) идут напрямую и без учётных данных.
"""

from __future__ import annotations

from http.cookiejar import CookieJar
import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = os.environ.get("VF_API", "https://localhost:8001").rstrip("/")
CA = os.environ.get("VF_CA", str(ROOT / "deploy" / "secrets" / "tls" / "server.crt"))
USER = os.environ.get("VF_USER", "admin")


def _password() -> str:
    if os.environ.get("VF_PASSWORD"):
        return os.environ["VF_PASSWORD"]
    env = ROOT / "deploy" / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            if line.startswith("VF_ADMIN_PASSWORD="):
                return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("Не задан пароль: VF_PASSWORD или VF_ADMIN_PASSWORD в deploy/.env")


class Session:
    def __init__(self) -> None:
        handlers = [urllib.request.HTTPCookieProcessor(CookieJar())]
        if API.startswith("https"):
            ctx = ssl.create_default_context(cafile=CA if os.path.isfile(CA) else None)
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self.csrf: str | None = None

    def _send(self, method, url, body, timeout, headers):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = dict(headers)
        if data:
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, None

    def login(self) -> None:
        code, data = self._send("POST", f"{API}/api/auth/login", {"username": USER, "password": _password()}, 15, {})
        if code != 200:
            raise RuntimeError(f"Не удалось войти как {USER}: {code} {data}")
        self.csrf = data["csrf_token"]

    def request(self, method, url, body=None, timeout=10):
        headers = {"X-CSRF-Token": self.csrf} if self.csrf else {}
        code, data = self._send(method, url, body, timeout, headers)
        if code == 401:  # сессия истекла или координатор перезапускался с новым ключом
            self.login()
            code, data = self._send(method, url, body, timeout, {"X-CSRF-Token": self.csrf})
        return code, data


_session: Session | None = None


def http(method, url, body=None, timeout=10):
    """Как urllib, но для адресов координатора выполняет вход и добавляет CSRF-токен."""
    global _session
    if not url.startswith(API):
        return _plain(method, url, body, timeout)
    if url.startswith(f"{API}/healthz") or url.startswith(f"{API}/readyz"):
        return _anon(method, url, body, timeout)
    if _session is None:
        _session = Session()
        _session.login()
    return _session.request(method, url, body, timeout)


def _anon(method, url, body, timeout):
    return Session()._send(method, url, body, timeout, {})


def _plain(method, url, body, timeout):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, None
