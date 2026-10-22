"""HTTP-эндпоинты управления агентом (SSH подменён заглушкой)."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.deploy import DeployConfig, Result, SshDeployer


class FakeRunner:
    def __init__(self):
        self.calls, self.queue = [], []

    def __call__(self, args, stdin, timeout):
        self.calls.append(args)
        return self.queue.pop(0) if self.queue else Result(0, "3.12.1\n", "")


@pytest.fixture
def ctx(session_factory, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "worker-key").write_text("PRIVATE-KEY-CONTENT")
    src = tmp_path / "agent" / "videofetch_agent"
    src.mkdir(parents=True)
    (src / "main.py").write_text("x = 1\n")
    runner = FakeRunner()
    cfg = DeployConfig(source_dir=str(tmp_path / "agent"), coordinator_url="http://coordinator:8000", key_dir=str(secrets))
    app = create_app(session_factory, background=False, deployer=SshDeployer(cfg, runner), settings=Settings(auth_enabled=False))
    with TestClient(app) as c:
        yield c, runner, str(secrets / "worker-key")


def add_worker(c, key):
    body = {"name": "w1", "host": "worker-01", "ssh_user": "downloader", "storage_path": "/data/videos"}
    if key:
        body["ssh_key_ref"] = key
    return c.post("/api/workers", json=body).json()["id"]


def test_deploy_start_status_stop(ctx):
    c, runner, key = ctx
    wid = add_worker(c, key)
    assert c.post(f"/api/workers/{wid}/agent/check").json() == {"ok": True, "python": "3.12.1"}
    r = c.post(f"/api/workers/{wid}/agent/deploy")
    assert r.status_code == 200 and r.json()["deployed"] is True
    runner.queue = [Result(0, "started\n", "")]
    assert c.post(f"/api/workers/{wid}/agent/start").json() == {"state": "started"}
    runner.queue = [Result(0, "running\nabc\nlog line\n", "")]
    assert c.get(f"/api/workers/{wid}/agent").json()["state"] == "running"
    runner.queue = [Result(0, "stopped\n", "")]
    assert c.post(f"/api/workers/{wid}/agent/stop").json() == {"state": "stopped"}


def test_operations_are_logged_without_secrets(ctx):
    c, runner, key = ctx
    wid = add_worker(c, key)
    c.post(f"/api/workers/{wid}/agent/deploy")
    runner.queue = [Result(255, "", "ssh: connect to host worker-01 port 22: Connection refused")]
    r = c.post(f"/api/workers/{wid}/agent/start")
    assert r.status_code == 502 and r.json()["error"] == "ssh_unreachable"
    body = r.text + str(runner.calls)
    assert "PRIVATE-KEY-CONTENT" not in body


def test_worker_without_key_cannot_be_deployed(ctx):
    c, _, _ = ctx
    wid = add_worker(c, None)
    r = c.post(f"/api/workers/{wid}/agent/deploy")
    assert r.status_code == 422 and r.json()["error"] == "no_ssh_key"


def test_key_outside_secrets_rejected(ctx):
    c, _, _ = ctx
    wid = add_worker(c, "/etc/passwd")
    r = c.post(f"/api/workers/{wid}/agent/check")
    assert r.status_code == 422 and r.json()["error"] == "key_outside_secrets"


def test_unknown_worker(ctx):
    c, _, _ = ctx
    assert c.post("/api/workers/999/agent/deploy").status_code == 404


def test_key_ref_is_not_exposed_in_worker_api(ctx):
    c, _, key = ctx
    wid = add_worker(c, key)
    assert "ssh_key_ref" not in c.get(f"/api/workers/{wid}").json()
