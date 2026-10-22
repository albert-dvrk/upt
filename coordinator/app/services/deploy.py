"""Развёртывание и управление агентом на рабочих серверах через SSH.

Используется системный клиент `ssh` (без паролей, только ключи). Закрытый ключ в БД не хранится:
у сервера есть лишь ссылка `ssh_key_ref` — путь к файлу секрета внутри разрешённого каталога.
Все значения, попадающие в команду, проверяются по строгим шаблонам и экранируются.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shlex
import subprocess
import tarfile
from dataclasses import dataclass
from typing import Callable

from app.services.errors import Invalid, RemoteError

_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DIR_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    user: str
    key_ref: str | None
    name: str


@dataclass
class DeployConfig:
    source_dir: str  # каталог, содержащий пакет videofetch_agent
    coordinator_url: str  # адрес координатора, как его видят агенты
    key_dir: str = "/run/secrets"
    remote_dir: str = "videofetch-agent"  # относительно домашнего каталога на сервере
    python: str = "python3"
    known_hosts: str | None = None
    connect_timeout: int = 10
    url_template: str = ""
    command_timeout: int = 60


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str], bytes | None, float], Result]


def subprocess_runner(args: list[str], stdin: bytes | None, timeout: float) -> Result:
    try:
        p = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RemoteError("Превышено время ожидания SSH-команды", "ssh_timeout") from exc
    except FileNotFoundError as exc:
        raise RemoteError("На координаторе не установлен клиент ssh", "ssh_missing") from exc
    return Result(p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace"))


class SshDeployer:
    def __init__(self, cfg: DeployConfig, runner: Runner = subprocess_runner) -> None:
        if not _DIR_RE.match(cfg.remote_dir) or ".." in cfg.remote_dir.split("/"):
            raise ValueError("Недопустимый remote_dir")
        self.cfg = cfg
        self.runner = runner

    # ---------------------------------------------------------------- проверки входных данных

    def validate(self, t: Target) -> str:
        """Проверяет данные сервера и возвращает абсолютный путь к ключу."""
        if not _HOST_RE.match(t.host):
            raise Invalid("Недопустимое имя хоста", "bad_host")
        if not _USER_RE.match(t.user):
            raise Invalid("Недопустимое имя пользователя SSH", "bad_ssh_user")
        if not _NAME_RE.match(t.name):
            raise Invalid("Недопустимое имя сервера", "bad_worker_name")
        if not (1 <= t.port <= 65535):
            raise Invalid("Недопустимый порт SSH", "bad_port")
        if not t.key_ref:
            raise Invalid("Для сервера не задан ключ (ssh_key_ref)", "no_ssh_key")
        key = os.path.realpath(t.key_ref)
        root = os.path.realpath(self.cfg.key_dir)
        if not key.startswith(root + os.sep):
            raise Invalid("Ключ должен находиться в каталоге секретов", "key_outside_secrets")
        if not os.path.isfile(key):
            raise Invalid("Файл ключа не найден на координаторе", "key_missing")
        return key

    # ---------------------------------------------------------------- SSH

    def ssh_command(self, t: Target, key: str, remote: str) -> list[str]:
        known = self.cfg.known_hosts or "/dev/null"
        return [
            "ssh",
            "-i", key,
            "-p", str(t.port),
            "-o", "BatchMode=yes",  # никаких запросов пароля
            "-o", "IdentitiesOnly=yes",
            "-o", f"ConnectTimeout={self.cfg.connect_timeout}",
            "-o", "StrictHostKeyChecking=accept-new",  # первый раз запоминаем отпечаток, при смене — отказ
            "-o", f"UserKnownHostsFile={known}",
            "-o", "LogLevel=ERROR",
            f"{t.user}@{t.host}",
            remote,
        ]

    def run(self, t: Target, remote: str, stdin: bytes | None = None, timeout: float | None = None) -> Result:
        key = self.validate(t)
        res = self.runner(self.ssh_command(t, key, remote), stdin, timeout or self.cfg.command_timeout)
        if res.returncode == 255:  # код самого ssh: не подключились / отказ в доступе
            raise RemoteError(f"SSH: {res.stderr.strip()[:300] or 'не удалось подключиться'}", "ssh_unreachable")
        return res

    def _d(self) -> str:
        return shlex.quote(self.cfg.remote_dir)

    # ---------------------------------------------------------------- операции

    def check(self, t: Target) -> dict:
        res = self.run(t, f"{shlex.quote(self.cfg.python)} -c 'import sys;print(sys.version.split()[0])'")
        if res.returncode != 0:
            raise RemoteError("На сервере не найден Python 3", "python_missing")
        return {"ok": True, "python": res.stdout.strip()}

    def build_archive(self) -> tuple[bytes, str]:
        """tar.gz пакета агента (воспроизводимый: без времён и владельцев) и его SHA-256."""
        pkg = os.path.join(self.cfg.source_dir, "videofetch_agent")
        if not os.path.isdir(pkg):
            raise RemoteError("На координаторе нет исходников агента", "agent_source_missing")
        buf = io.BytesIO()

        def norm(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
            if "__pycache__" in info.name or info.name.endswith(".pyc"):
                return None
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            return info

        with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
            for root, dirs, files in os.walk(pkg):
                dirs.sort()
                for fn in sorted(files):
                    full = os.path.join(root, fn)
                    arc = os.path.relpath(full, self.cfg.source_dir)
                    info = norm(tar.gettarinfo(full, arcname=arc))
                    if info:
                        with open(full, "rb") as fh:
                            tar.addfile(info, fh)
        data = buf.getvalue()
        return data, hashlib.sha256(data).hexdigest()

    def deploy(self, t: Target) -> dict:
        self.check(t)
        data, digest = self.build_archive()
        d = self._d()
        res = self.run(
            t,
            f"mkdir -p {d} && rm -rf {d}/videofetch_agent && tar xzf - -C {d} && echo {digest} > {d}/.deployed_sha256",
            stdin=data,
        )
        if res.returncode != 0:
            raise RemoteError(f"Не удалось распаковать агент: {res.stderr.strip()[:300]}", "deploy_failed")
        return {"deployed": True, "sha256": digest, "bytes": len(data)}

    def start(self, t: Target, token: str | None = None, ca_pem: str | None = None) -> dict:
        """Запускает агента. Токен и сертификат передаются через stdin, а не в командной строке
        (аргументы видны в `ps`). Если агент уже работает, он останавливается: старый токен недействителен."""
        d = self._d()
        flags = []
        if token:
            flags += ["--token-file", "agent.token"]
        if ca_pem:
            flags += ["--ca-file", "ca.pem"]
        agent_cmd = " ".join(
            shlex.quote(x)
            for x in [
                self.cfg.python, "-m", "videofetch_agent",
                "--coordinator", self.cfg.coordinator_url,
                "--name", t.name,
                *flags,
                *(["--url-template", self.cfg.url_template] if self.cfg.url_template else []),
            ]
        )
        payload = b""
        prep = ""
        if token or ca_pem:
            # stdin: строка токена, затем PEM. Файлы создаются с правами 0600.
            prep = (
                "umask 077; IFS= read -r tok; "
                + ("printf '%s' \"$tok\" > agent.token; " if token else "")
                + ("cat > ca.pem; " if ca_pem else "cat > /dev/null; ")
            )
            payload = ((token or "") + "\n").encode() + (ca_pem or "").encode()
        stop_old = (
            "if [ -f agent.pid ] && kill -0 \"$(cat agent.pid)\" 2>/dev/null; then "
            "kill \"$(cat agent.pid)\"; for i in 1 2 3 4 5 6 7 8 9 10; do "
            "kill -0 \"$(cat agent.pid)\" 2>/dev/null || break; sleep 1; done; "
            "kill -9 \"$(cat agent.pid)\" 2>/dev/null; fi; "
        )
        keep_running = (
            "if [ -f agent.pid ] && kill -0 \"$(cat agent.pid)\" 2>/dev/null; then echo already_running; exit 0; fi; "
        )
        script = (
            f"cd {d} || exit 3; {prep}"
            f"{stop_old if token else keep_running}"
            f"nohup {agent_cmd} >> agent.log 2>&1 < /dev/null & echo $! > agent.pid; sleep 1; "
            "if kill -0 \"$(cat agent.pid)\" 2>/dev/null; then echo started; else echo crashed; exit 4; fi"
        )
        res = self.run(t, script, stdin=payload or None)
        if res.returncode == 3:
            raise RemoteError("Агент не развёрнут на сервере (сначала deploy)", "not_deployed")
        if res.returncode != 0:
            raise RemoteError("Агент сразу завершился; смотрите agent.log на сервере", "agent_crashed")
        return {"state": res.stdout.strip().splitlines()[-1]}

    def stop(self, t: Target) -> dict:
        d = self._d()
        script = (
            f"cd {d} 2>/dev/null || {{ echo not_deployed; exit 0; }}; "
            "[ -f agent.pid ] || { echo not_running; exit 0; }; "
            "pid=$(cat agent.pid); "
            "if kill -0 \"$pid\" 2>/dev/null; then kill \"$pid\"; "
            "for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 \"$pid\" 2>/dev/null || break; sleep 1; done; "
            "kill -0 \"$pid\" 2>/dev/null && kill -9 \"$pid\"; echo stopped; else echo not_running; fi; rm -f agent.pid"
        )
        res = self.run(t, script, timeout=30)
        return {"state": res.stdout.strip().splitlines()[-1] if res.stdout.strip() else "unknown"}

    def status(self, t: Target) -> dict:
        d = self._d()
        script = (
            f"cd {d} 2>/dev/null || {{ echo not_deployed; exit 0; }}; "
            "if [ -f agent.pid ] && kill -0 \"$(cat agent.pid)\" 2>/dev/null; then echo running; else echo stopped; fi; "
            "cat .deployed_sha256 2>/dev/null || echo -; tail -n 5 agent.log 2>/dev/null"
        )
        res = self.run(t, script)
        lines = res.stdout.splitlines()
        state = lines[0] if lines else "unknown"
        return {
            "state": state,
            "deployed_sha256": (lines[1] if len(lines) > 1 and lines[1] != "-" else None),
            "log_tail": lines[2:],
        }
