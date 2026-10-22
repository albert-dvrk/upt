"""Аутентификация, роли, CSRF, токены агентов и журнал аудита (HTTP)."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.deploy import DeployConfig, Result, SshDeployer

ADMIN_PW = "Admin-pass-1234"
WORKER = {"name": "w1", "host": "10.0.0.1", "ssh_user": "downloader", "storage_path": "/data/videos"}


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, args, stdin, timeout):
        self.calls.append((args, stdin))
        return Result(0, "started\n", "")


@pytest.fixture
def env(session_factory, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "key").write_text("K")
    pkg = tmp_path / "src" / "videofetch_agent"
    pkg.mkdir(parents=True)
    (pkg / "main.py").write_text("x = 1\n")
    ca = tmp_path / "ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nZm9v\n-----END CERTIFICATE-----\n")
    runner = FakeRunner()
    cfg = DeployConfig(source_dir=str(tmp_path / "src"), coordinator_url="https://coordinator:8000", key_dir=str(secrets))
    settings = Settings(auth_enabled=True, secret_key="k" * 32, admin_password=ADMIN_PW,
                        login_max_failures=3, login_lock_seconds=60, agent_ca_file=str(ca))
    app = create_app(session_factory, background=False, settings=settings, deployer=SshDeployer(cfg, runner))
    with TestClient(app) as anon:
        yield anon, app, runner, str(secrets / "key")


def login(app, username="admin", password=ADMIN_PW):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


def make_user(admin, name, role, password="Passw0rd-long"):
    r = admin.post("/api/users", json={"username": name, "password": password, "roles": [role]})
    assert r.status_code == 201, r.text


def test_everything_requires_login(env):
    anon, *_ = env
    for method, path in [("get", "/api/jobs"), ("get", "/api/workers"), ("post", "/api/jobs"),
                         ("get", "/api/users"), ("get", "/api/audit"), ("get", "/api/auth/me")]:
        r = getattr(anon, method)(path)
        assert r.status_code == 401 and r.json()["error"] == "unauthorized", path
    assert anon.get("/healthz").status_code == 200  # проверки живости открыты


def test_login_logout_and_cookie_flags(env):
    anon, app, *_ = env
    r = anon.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 200 and r.json()["roles"] == ["administrator"]
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "password" not in r.text
    assert anon.get("/api/auth/me").json()["username"] == "admin"
    anon.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    assert anon.post("/api/auth/logout").status_code == 200
    assert anon.get("/api/auth/me").status_code == 401


def test_bad_credentials_same_answer_for_unknown_user(env):
    anon, *_ = env
    a = anon.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})
    b = anon.post("/api/auth/login", json={"username": "nobody", "password": "wrong-password"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_login_is_throttled(env):
    anon, *_ = env
    for _ in range(3):
        assert anon.post("/api/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401
    r = anon.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 429 and r.json()["error"] == "too_many_attempts"


def test_roles_are_enforced(env):
    _, app, *_ = env
    admin = login(app)
    make_user(admin, "viewer1", "viewer")
    make_user(admin, "oper1", "operator")
    viewer, oper = login(app, "viewer1", "Passw0rd-long"), login(app, "oper1", "Passw0rd-long")

    assert viewer.get("/api/jobs").status_code == 200
    assert viewer.post("/api/jobs", json={"name": "x"}).status_code == 403
    assert viewer.get("/api/users").status_code == 403

    r = oper.post("/api/jobs", json={"name": "x"})
    assert r.status_code == 201
    assert oper.post("/api/workers", json=WORKER).status_code == 403  # серверы — только администратор
    assert oper.get("/api/audit").status_code == 403
    assert admin.post("/api/workers", json=WORKER).status_code == 201


def test_csrf_header_is_required_for_changes(env):
    _, app, *_ = env
    c = TestClient(app)
    c.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PW})  # cookie есть, заголовка нет
    r = c.post("/api/jobs", json={"name": "x"})
    assert r.status_code == 403 and r.json()["error"] == "csrf_failed"
    c.headers["X-CSRF-Token"] = "wrong"
    assert c.post("/api/jobs", json={"name": "x"}).status_code == 403
    assert c.get("/api/jobs").status_code == 200  # чтение CSRF не требует


def test_forged_cookie_is_rejected(env):
    _, app, *_ = env
    c = TestClient(app)
    c.cookies.set("vf_session", "forged.token.value")
    assert c.get("/api/jobs").status_code == 401


def test_disabled_user_loses_access_immediately(env):
    _, app, *_ = env
    admin = login(app)
    make_user(admin, "temp1", "operator")
    temp = login(app, "temp1", "Passw0rd-long")
    uid = next(u["id"] for u in admin.get("/api/users").json() if u["username"] == "temp1")
    assert admin.patch(f"/api/users/{uid}", json={"is_active": False}).status_code == 200
    assert temp.get("/api/jobs").status_code == 401


def test_last_administrator_is_protected(env):
    _, app, *_ = env
    admin = login(app)
    me = admin.get("/api/users").json()[0]
    r = admin.patch(f"/api/users/{me['id']}", json={"is_active": False})
    assert r.status_code == 409 and r.json()["error"] == "last_admin"
    r = admin.patch(f"/api/users/{me['id']}", json={"roles": ["viewer"]})
    assert r.status_code == 409
    assert admin.get("/api/auth/me").json()["roles"] == ["administrator"]


def test_user_validation(env):
    _, app, *_ = env
    admin = login(app)
    assert admin.post("/api/users", json={"username": "weak1", "password": "short"}).json()["error"] == "weak_password"
    assert admin.post("/api/users", json={"username": "x y", "password": "Passw0rd-long"}).status_code == 422
    assert admin.post("/api/users", json={"username": "okname", "password": "Passw0rd-long", "roles": ["root"]}).json()["error"] == "bad_role"
    make_user(admin, "dupl1", "viewer")
    assert admin.post("/api/users", json={"username": "DUPL1", "password": "Passw0rd-long"}).json()["error"] == "user_exists"
    assert "password" not in str(admin.get("/api/users").json())


def test_audit_records_actions_without_secrets(env):
    anon, app, *_ = env
    anon.post("/api/auth/login", json={"username": "admin", "password": "wrong-pass-1"})
    admin = login(app)
    make_user(admin, "oper2", "operator", password="Secret-value-777")
    jid = admin.post("/api/jobs", json={"name": "j"}).json()["id"]
    admin.post(f"/api/jobs/{jid}/stop")
    rows = admin.get("/api/audit").json()
    actions = [r["action"] for r in rows]
    for a in ("login_failed", "login", "user_create", "job_create", "job_stop", "bootstrap_admin"):
        assert a in actions, a
    dump = str(rows)
    assert "Secret-value-777" not in dump and "wrong-pass-1" not in dump
    assert admin.get("/api/audit", params={"action": "job_stop"}).json()[0]["target_id"] == str(jid)


def test_agent_protocol_needs_a_valid_token(env):
    anon, app, runner, key = env
    admin = login(app)
    wid = admin.post("/api/workers", json={**WORKER, "ssh_key_ref": key}).json()["id"]
    wid2 = admin.post("/api/workers", json={**WORKER, "name": "w2"}).json()["id"]

    assert anon.post("/api/workers/register", json={"name": "w1", "agent_version": "1"}).status_code == 401
    assert anon.post("/api/workers/register", json={"name": "ghost", "agent_version": "1"}).status_code == 401
    assert anon.post(f"/api/workers/{wid}/lease").status_code == 401

    # выпуск токена при запуске агента; он уходит на сервер через stdin, а не в аргументах
    assert admin.post(f"/api/workers/{wid}/agent/deploy").status_code == 200
    r = admin.post(f"/api/workers/{wid}/agent/start")
    assert r.status_code == 200
    args, stdin = runner.calls[-1]
    token = stdin.decode().splitlines()[0]
    assert len(token) >= 32 and token not in " ".join(args)
    assert "--token-file" in args[-1] and "--ca-file" in args[-1] and "BEGIN CERTIFICATE" in stdin.decode()

    bearer = {"Authorization": f"Bearer {token}"}
    ok = anon.post("/api/workers/register", json={"name": "w1", "agent_version": "1"}, headers=bearer)
    assert ok.status_code == 200 and ok.json()["worker_id"] == wid
    assert anon.post(f"/api/workers/{wid}/lease", headers=bearer).status_code == 200
    assert anon.post(f"/api/workers/{wid2}/lease", headers=bearer).status_code == 401  # чужой токен
    assert anon.post(f"/api/workers/{wid}/lease", headers={"Authorization": "Bearer nope"}).status_code == 401

    # перезапуск выпускает новый токен, старый перестаёт действовать
    admin.post(f"/api/workers/{wid}/agent/start")
    assert anon.post(f"/api/workers/{wid}/lease", headers=bearer).status_code == 401

    # отзыв
    token2 = runner.calls[-1][1].decode().splitlines()[0]
    h2 = {"Authorization": f"Bearer {token2}"}
    assert anon.post(f"/api/workers/{wid}/lease", headers=h2).status_code == 200
    assert admin.post(f"/api/workers/{wid}/token/revoke").status_code == 200
    assert anon.post(f"/api/workers/{wid}/lease", headers=h2).status_code == 401
    assert admin.get("/api/audit", params={"action": "agent_token_revoke"}).json()


def test_token_hash_not_exposed(env):
    _, app, runner, key = env
    admin = login(app)
    wid = admin.post("/api/workers", json={**WORKER, "ssh_key_ref": key}).json()["id"]
    admin.post(f"/api/workers/{wid}/agent/deploy")
    admin.post(f"/api/workers/{wid}/agent/start")
    token = runner.calls[-1][1].decode().splitlines()[0]
    body = admin.get(f"/api/workers/{wid}").text
    assert token not in body and "token" not in body


def test_security_headers(env):
    anon, *_ = env
    r = anon.get("/ui/")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert anon.get("/api/version").headers["cache-control"] == "no-store"
