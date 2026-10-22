#!/usr/bin/env python3
"""Проверка веб-терминала на живом стенде: настоящий SSH до worker-01 через WebSocket-шлюз координатора.

Запуск из каталога deploy/ (стенд поднят):   python3 ../loadtests/terminal_check.py
Нужен пакет websockets (он ставится вместе с uvicorn[standard], т.е. есть в виртуальном окружении проекта).
Проверяется: вход без cookie отклоняется; команда выполняется на удалённом сервере; опасная команда блокируется;
выход из оболочки закрывает сеанс; в журнале аудита есть открытие, команды, блокировка и закрытие.
"""

from __future__ import annotations

import json
import ssl
import sys
import time
import urllib.request

import vfclient
from vfclient import API, CA, http

try:
    from websockets.sync.client import connect
except ImportError:  # pragma: no cover
    print("Не найден пакет websockets: pip install websockets")
    raise SystemExit(2)

WS = API.replace("https://", "wss://").replace("http://", "ws://")
problems: list[str] = []


def ctx():
    return ssl.create_default_context(cafile=CA) if API.startswith("https") else None


def cookie_value() -> str:
    http("GET", f"{API}/api/workers")  # гарантирует вход
    for h in vfclient._session.opener.handlers:
        jar = getattr(h, "cookiejar", None)
        if jar is not None:
            for c in jar:
                if c.name == "vf_session":
                    return c.value
    raise RuntimeError("cookie сессии не найдена")


def open_ws(path, headers):
    try:
        return connect(f"{WS}{path}", ssl=ctx(), additional_headers=headers, open_timeout=15)
    except TypeError:  # старые версии websockets
        return connect(f"{WS}{path}", ssl=ctx(), extra_headers=headers, open_timeout=15)


def read_until(ws, pred, timeout=20):
    end, seen = time.time() + timeout, []
    while time.time() < end:
        try:
            raw = ws.recv(timeout=max(0.1, end - time.time()))
        except TimeoutError:
            break
        msg = json.loads(raw)
        seen.append(msg)
        if pred(msg, seen):
            return seen
    raise AssertionError(f"не дождались ответа; последнее: {seen[-3:]}")


def output_has(text):
    return lambda m, seen: "".join(x.get("data", "") for x in seen if x["type"] == "output").count(text) >= 1


def main() -> int:
    workers = http("GET", f"{API}/api/workers")[1]
    wid = next(w["id"] for w in workers if w["name"] == "worker-01")
    path = f"/api/workers/{wid}/terminal"

    # 1. без cookie сеанс не открывается
    try:
        with open_ws(path, {"Origin": API}):
            problems.append("терминал открылся без входа")
    except Exception:  # noqa: BLE001 — ожидаемый отказ рукопожатия
        pass

    headers = {"Cookie": f"vf_session={cookie_value()}", "Origin": API}

    # 2. чужой Origin отклоняется
    try:
        with open_ws(path, {"Cookie": headers["Cookie"], "Origin": "https://evil.example"}):
            problems.append("терминал открылся с чужим Origin")
    except Exception:  # noqa: BLE001
        pass

    # 3. настоящий сеанс
    with open_ws(path, headers) as ws:
        read_until(ws, lambda m, s: m["type"] == "ready")
        ws.send(json.dumps({"type": "resize", "rows": 30, "cols": 100}))
        time.sleep(1.0)  # приглашение оболочки
        ws.send(json.dumps({"type": "input", "data": "echo vf-$((6*7))\r"}))
        read_until(ws, output_has("vf-42"))
        ws.send(json.dumps({"type": "input", "data": "id -un | tr a-z A-Z\r"}))
        read_until(ws, output_has("DOWNLOADER"))
        ws.send(json.dumps({"type": "input", "data": "sudo id\r"}))
        notes = read_until(ws, lambda m, s: m["type"] == "notice")
        if "privilege_escalation" not in notes[-1]["message"]:
            problems.append(f"sudo заблокирован по другой причине: {notes[-1]['message']}")
        ws.send(json.dumps({"type": "input", "data": "exit\r"}))
        closed = read_until(ws, lambda m, s: m["type"] == "closed")
        if closed[-1]["reason"] != "ssh_exited":
            problems.append(f"причина закрытия {closed[-1]['reason']}")

    # 4. аудит
    time.sleep(1.0)
    for action in ("terminal_open", "terminal_command", "terminal_blocked", "terminal_close"):
        rows = http("GET", f"{API}/api/audit?action={action}&limit=50")[1] or []
        if not any(r["target_id"] == str(wid) for r in rows):
            problems.append(f"в журнале аудита нет записи {action}")
    cmds = [r["details"]["command"] for r in (http("GET", f"{API}/api/audit?action=terminal_command&limit=50")[1] or [])]
    if not any("vf-$((6*7))" in c for c in cmds):
        problems.append("выполненная команда не попала в журнал")

    print("PASS" if not problems else "FAIL")
    for p in problems:
        print("  -", p)
    return 0 if not problems else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print("FAIL\n  -", exc)
        sys.exit(1)
