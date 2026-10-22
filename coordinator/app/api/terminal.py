"""Веб-терминал SSH: WebSocket-шлюз координатора (только администраторы).

Браузер ключей не видит: ssh запускает координатор. Сеанс короткий (простой и общий предел),
права перепроверяются по БД во время работы, открытие, команды, блокировки и закрытие пишутся
в журнал аудита (значения, похожие на пароли и токены, маскируются; вывод терминала не журналируется).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import urlparse

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.api.deps import authenticate_user
from app.models import Worker
from app.security import has_role
from app.services import auth as auth_svc
from app.services.deploy import Target
from app.services.errors import Forbidden, NotFound, ServiceError
from app.services.terminal import CommandPolicy, PtyProcess, SecretPromptDetector

log = logging.getLogger("videofetch.terminal")
router = APIRouter(prefix="/api/workers", tags=["terminal"])

MAX_MESSAGE = 16 * 1024
MAX_INPUT = 8 * 1024
RECHECK_SECONDS = 15


def _authorize(websocket: WebSocket, worker_id: int):
    """(принципал, цель SSH, имя сервера) либо ServiceError. Синхронная: ходит в БД."""
    with websocket.app.state.session_factory() as session:
        principal = authenticate_user(websocket, session, check_csrf=False)
        if not has_role(principal.roles, "administrator"):
            raise Forbidden("Терминал доступен только администраторам")
        worker = session.get(Worker, worker_id)
        if worker is None:
            raise NotFound("Сервер не найден")
        if not worker.enabled:
            raise Forbidden("Сервер отключён", "worker_disabled")
        target = Target(worker.host, worker.ssh_port, worker.ssh_user, worker.ssh_key_ref, worker.name)
        return principal, target


def _audit(factory, principal, action: str, worker_id: int, details: dict) -> None:
    try:
        with factory() as session:
            auth_svc.audit(session, principal, action, "worker", worker_id, ip=principal.ip, details=details)
            session.commit()
    except Exception:  # noqa: BLE001 — сбой журнала не должен ронять сеанс; ошибка уйдёт в лог
        log.exception("не удалось записать аудит терминала")


def _same_origin(websocket: WebSocket) -> bool:
    """Браузер всегда шлёт Origin; он обязан совпадать с хостом запроса (защита от межсайтового подключения)."""
    origin = websocket.headers.get("origin")
    if origin is None:
        return True  # не браузер; доступ всё равно требует cookie сессии
    host = websocket.headers.get("host", "")
    return urlparse(origin).netloc.lower() == host.lower()


async def _send(websocket: WebSocket, **msg) -> None:
    await websocket.send_text(json.dumps(msg, ensure_ascii=False))


@router.websocket("/{worker_id}/terminal")
async def terminal(websocket: WebSocket, worker_id: int):
    app = websocket.app
    settings = app.state.settings
    factory = app.state.session_factory
    if not settings.terminal_enabled or not _same_origin(websocket):
        await websocket.close(code=4403)
        return
    try:
        principal, target = await run_in_threadpool(_authorize, websocket, worker_id)
    except ServiceError as exc:
        await websocket.close(code=4401 if exc.status_code == 401 else 4403 if exc.status_code == 403 else 4404)
        return
    active = getattr(app.state, "terminal_sessions", 0)
    if active >= settings.terminal_max_sessions:
        await websocket.close(code=4429)
        return
    try:
        argv = app.state.deployer.interactive_argv(target)
    except ServiceError as exc:
        await websocket.accept()
        await _send(websocket, type="error", message=exc.message)
        await websocket.close(code=4400)
        return

    app.state.terminal_sessions = active + 1
    await websocket.accept()
    started = time.monotonic()
    last_input = started
    reason = "client_closed"
    pty_proc = PtyProcess(argv)
    policy = CommandPolicy()
    prompt = SecretPromptDetector()  # локальный PTY у ssh всегда «сырой», поэтому запрос пароля определяем по выводу
    logged = {"n": 0}
    try:
        try:
            await pty_proc.start()
        except OSError as exc:
            await _send(websocket, type="error", message=f"Не удалось запустить ssh: {exc.strerror or exc}")
            reason = "spawn_failed"
            return
        await run_in_threadpool(_audit, factory, principal, "terminal_open", worker_id,
                                {"server": target.name, "idle_limit": settings.terminal_idle_seconds, "max_seconds": settings.terminal_max_seconds})
        await _send(websocket, type="ready", server=target.name, idle_seconds=settings.terminal_idle_seconds,
                    max_seconds=settings.terminal_max_seconds)

        async def pump_output() -> str:
            while True:
                data = await pty_proc.output.get()
                if data is None:
                    return "ssh_exited"
                text = pty_proc.decode(data)
                if text:
                    prompt.feed_output(text)
                    await _send(websocket, type="output", data=text)

        async def pump_input() -> str:
            nonlocal last_input
            while True:
                try:
                    raw = await websocket.receive_text()
                except WebSocketDisconnect:
                    return "client_closed"
                if len(raw) > MAX_MESSAGE:
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                kind = msg.get("type") if isinstance(msg, dict) else None
                if kind == "resize":
                    rows, cols = msg.get("rows"), msg.get("cols")
                    if isinstance(rows, int) and isinstance(cols, int):
                        pty_proc.resize(rows, cols)
                elif kind == "input":
                    data = msg.get("data")
                    if not isinstance(data, str) or len(data) > MAX_INPUT:
                        continue
                    last_input = time.monotonic()
                    fed = policy.feed(data, echo=not prompt.active)
                    prompt.feed_input(data)
                    for rule, line in fed.blocked:
                        await run_in_threadpool(_audit, factory, principal, "terminal_blocked", worker_id,
                                                {"server": target.name, "rule": rule, "command": line})
                        await _send(websocket, type="notice", message=f"Команда заблокирована политикой безопасности ({rule})")
                    for line in fed.executed:
                        if logged["n"] < settings.terminal_max_commands_logged:
                            logged["n"] += 1
                            await run_in_threadpool(_audit, factory, principal, "terminal_command", worker_id,
                                                    {"server": target.name, "command": line})
                        elif logged["n"] == settings.terminal_max_commands_logged:
                            logged["n"] += 1
                            await run_in_threadpool(_audit, factory, principal, "terminal_command_log_truncated", worker_id,
                                                    {"server": target.name, "limit": settings.terminal_max_commands_logged})
                    pty_proc.write(fed.forward)

        async def watchdog() -> str:
            next_check = time.monotonic() + RECHECK_SECONDS
            while True:
                await asyncio.sleep(1)
                now = time.monotonic()
                if now - started > settings.terminal_max_seconds:
                    return "max_duration"
                if now - last_input > settings.terminal_idle_seconds:
                    return "idle_timeout"
                if now >= next_check:
                    next_check = now + RECHECK_SECONDS
                    try:  # права могли отозвать, пока сеанс открыт
                        await run_in_threadpool(_authorize, websocket, worker_id)
                    except ServiceError:
                        return "access_revoked"

        tasks = [asyncio.create_task(f()) for f in (pump_output, pump_input, watchdog)]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() is None:
                    reason = t.result()
                else:
                    reason = "error"
                    log.warning("сеанс терминала завершён ошибкой: %r", t.exception())
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        notice = {
            "idle_timeout": "Сеанс закрыт из-за неактивности",
            "max_duration": "Достигнута предельная длительность сеанса",
            "access_revoked": "Доступ отозван",
            "ssh_exited": "Соединение SSH завершено",
        }.get(reason)
        if notice:
            try:
                await _send(websocket, type="closed", reason=reason, message=notice)
            except Exception:  # noqa: BLE001 — клиент уже ушёл
                pass
    finally:
        # Процесс и место в лимите освобождаются синхронно, без await: при отмене задачи (отключение клиента,
        # остановка сервера) очистка не должна прерываться. Запись в аудит идёт в потоке под shield:
        # она доходит до конца, даже если задачу отменили.
        code = pty_proc.kill()
        app.state.terminal_sessions = max(0, getattr(app.state, "terminal_sessions", 1) - 1)
        close_details = {"server": target.name, "reason": reason, "seconds": round(time.monotonic() - started),
                         "commands": logged["n"], "exit_code": code}
        try:
            await asyncio.shield(run_in_threadpool(_audit, factory, principal, "terminal_close", worker_id, close_details))
        except BaseException:  # noqa: BLE001 — отмена: поток всё равно допишет запись
            pass
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 — уже закрыт
            pass
