import io
import os
import tarfile

import pytest

from app.services.deploy import DeployConfig, Result, SshDeployer, Target
from app.services.errors import Invalid, RemoteError


class FakeRunner:
    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def __call__(self, args, stdin, timeout):
        self.calls.append((args, stdin, timeout))
        return self.results.pop(0) if self.results else Result(0, "3.12.1\n", "")


@pytest.fixture
def env(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    key = secrets / "key"
    key.write_text("PRIVATE")
    src = tmp_path / "agent" / "videofetch_agent"
    src.mkdir(parents=True)
    (src / "main.py").write_text("print(1)\n")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.pyc").write_bytes(b"junk")
    cfg = DeployConfig(source_dir=str(tmp_path / "agent"), coordinator_url="http://coordinator:8000", key_dir=str(secrets), url_template="http://mock:8080/{id}")
    target = Target(host="worker-01", port=22, user="downloader", key_ref=str(key), name="worker-01")
    return cfg, target, tmp_path


def test_archive_is_reproducible_and_clean(env):
    cfg, _, _ = env
    d = SshDeployer(cfg, FakeRunner())
    data1, sha1 = d.build_archive()
    data2, sha2 = d.build_archive()
    assert sha1 == sha2
    names = tarfile.open(fileobj=io.BytesIO(data1), mode="r:gz").getnames()
    assert "videofetch_agent/main.py" in names and not any("pycache" in n for n in names)


def test_deploy_uploads_archive_over_stdin(env):
    cfg, t, _ = env
    runner = FakeRunner()
    out = SshDeployer(cfg, runner).deploy(t)
    assert out["deployed"] and len(out["sha256"]) == 64
    args, stdin, _ = runner.calls[-1]
    assert args[0] == "ssh" and "BatchMode=yes" in args and args[-2] == "downloader@worker-01"
    assert stdin[:2] == b"\x1f\x8b"  # gzip


def test_private_key_content_never_in_command(env):
    cfg, t, _ = env
    runner = FakeRunner()
    SshDeployer(cfg, runner).deploy(t)
    assert all("PRIVATE" not in " ".join(a) for a, _, _ in runner.calls)


@pytest.mark.parametrize("host", ["-oProxyCommand=evil", "a b", "host;rm -rf /", "", "$(x)"])
def test_bad_hosts_rejected(env, host):
    cfg, t, _ = env
    bad = Target(host, t.port, t.user, t.key_ref, t.name)
    with pytest.raises(Invalid):
        SshDeployer(cfg, FakeRunner()).run(bad, "true")


@pytest.mark.parametrize("user", ["-x", "a;b", "Root", "u u", ""])
def test_bad_users_rejected(env, user):
    cfg, t, _ = env
    with pytest.raises(Invalid):
        SshDeployer(cfg, FakeRunner()).run(Target(t.host, t.port, user, t.key_ref, t.name), "true")


def test_key_must_be_inside_secrets_dir(env):
    cfg, t, tmp = env
    outside = tmp / "other_key"
    outside.write_text("x")
    for ref in (str(outside), str(tmp / "secrets" / ".." / "other_key"), "/etc/passwd"):
        with pytest.raises(Invalid) as exc:
            SshDeployer(cfg, FakeRunner()).run(Target(t.host, t.port, t.user, ref, t.name), "true")
        assert exc.value.code == "key_outside_secrets"
    with pytest.raises(Invalid) as exc:
        SshDeployer(cfg, FakeRunner()).run(Target(t.host, t.port, t.user, None, t.name), "true")
    assert exc.value.code == "no_ssh_key"


def test_symlink_escape_blocked(env):
    cfg, t, tmp = env
    outside = tmp / "evil"
    outside.write_text("x")
    link = tmp / "secrets" / "link"
    os.symlink(outside, link)
    with pytest.raises(Invalid):
        SshDeployer(cfg, FakeRunner()).run(Target(t.host, t.port, t.user, str(link), t.name), "true")


def test_unreachable_server_is_reported(env):
    cfg, t, _ = env
    runner = FakeRunner([Result(255, "", "ssh: connect to host worker-01 port 22: Connection refused")])
    with pytest.raises(RemoteError) as exc:
        SshDeployer(cfg, runner).check(t)
    assert exc.value.code == "ssh_unreachable" and "Connection refused" in exc.value.message


def test_missing_python(env):
    cfg, t, _ = env
    with pytest.raises(RemoteError) as exc:
        SshDeployer(cfg, FakeRunner([Result(127, "", "not found")])).check(t)
    assert exc.value.code == "python_missing"


def test_start_command_is_quoted_and_complete(env):
    cfg, t, _ = env
    runner = FakeRunner([Result(0, "started\n", "")])
    assert SshDeployer(cfg, runner).start(t)["state"] == "started"
    remote = runner.calls[0][0][-1]
    assert "--coordinator http://coordinator:8000" in remote and "--name worker-01" in remote
    assert "'http://mock:8080/{id}'" in remote  # фигурные скобки экранированы
    assert "nohup" in remote and "agent.pid" in remote


def test_start_failures(env):
    cfg, t, _ = env
    with pytest.raises(RemoteError) as exc:
        SshDeployer(cfg, FakeRunner([Result(3, "", "")])).start(t)
    assert exc.value.code == "not_deployed"
    with pytest.raises(RemoteError) as exc:
        SshDeployer(cfg, FakeRunner([Result(4, "crashed\n", "")])).start(t)
    assert exc.value.code == "agent_crashed"


def test_status_parsing(env):
    cfg, t, _ = env
    out = SshDeployer(cfg, FakeRunner([Result(0, "running\nabc123\nline1\nline2\n", "")])).status(t)
    assert out == {"state": "running", "deployed_sha256": "abc123", "log_tail": ["line1", "line2"]}
    out = SshDeployer(cfg, FakeRunner([Result(0, "not_deployed\n", "")])).status(t)
    assert out["state"] == "not_deployed" and out["deployed_sha256"] is None


def test_stop(env):
    cfg, t, _ = env
    assert SshDeployer(cfg, FakeRunner([Result(0, "stopped\n", "")])).stop(t)["state"] == "stopped"


def test_remote_dir_validation(env):
    cfg, _, _ = env
    for bad in ("../x", "a b", "/abs", "a;b", ""):
        cfg.remote_dir = bad
        with pytest.raises(ValueError):
            SshDeployer(cfg, FakeRunner())
