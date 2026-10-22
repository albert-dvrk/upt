#!/usr/bin/env python3
"""Сценарии отказов на живом стенде (docker compose). Только стандартная библиотека.

Запуск из каталога deploy/:   python3 ../loadtests/chaos.py [сценарий ...]
Учётные данные и адрес — см. vfclient.py (по умолчанию HTTPS, admin из deploy/.env).
Сценарии: baseline flaky_mock kill_agent coordinator_restart postgres_restart stop_resume
          rate_limit_429 disk_full corrupt_part network_drop  (по умолчанию — все)

Для каждого сценария создаётся задание на своём диапазоне идентификаторов, во время работы
вносится сбой, затем проверяется итог: задание завершилось, результат есть для каждого ID, статусы
совпадают с «истиной» mock-сервиса, скачанные файлы целы (SHA-256 из метаданных mock).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from vfclient import API, http  # вход, HTTPS и CSRF — в общем клиенте

MOCK = "http://localhost:8080"
WORKERS = ["worker-01", "worker-02", "worker-03"]


# ------------------------------------------------------------------ вспомогательное


def dc(*args, check=True):
    deploy_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "deploy")  # compose-файл лежит там
    return subprocess.run(["docker", "compose", *args], capture_output=True, text=True, check=check, cwd=deploy_dir)


def wait_healthy(timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if http("GET", f"{API}/healthz", timeout=3)[0] == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    raise RuntimeError("координатор не поднялся")


def worker_ids():
    return {w["name"]: w["id"] for w in http("GET", f"{API}/api/workers")[1]}


def ensure_agents():
    """Серверы созданы, агенты развёрнуты и запущены."""
    ids = worker_ids()
    for name in WORKERS:
        if name not in ids:
            http("POST", f"{API}/api/workers", {"name": name, "host": name, "ssh_user": "downloader",
                 "ssh_key_ref": "/run/secrets/worker-key", "storage_path": "/data/videos", "threads": 8, "min_free_space_gb": 0})
    ids = worker_ids()
    for name, wid in ids.items():
        http("POST", f"{API}/api/workers/{wid}/agent/stop", timeout=60)  # чтобы запустился свежий код
        http("POST", f"{API}/api/workers/{wid}/agent/deploy", timeout=60)
        http("POST", f"{API}/api/workers/{wid}/agent/start", timeout=60)
    # ждём, пока все станут online
    end = time.time() + 40
    while time.time() < end:
        st = {w["name"]: w["status"] for w in http("GET", f"{API}/api/workers")[1]}
        if all(st.get(n) == "online" for n in WORKERS):
            return ids
        time.sleep(1)
    raise RuntimeError(f"не все серверы online: {st}")


def mock_config(**kw):
    http("POST", f"{MOCK}/_control/config", kw)


def mock_reset():
    http("POST", f"{MOCK}/_control/reset")


def create_job(name, lo, hi, chunk=50, workers=None):
    ids = worker_ids()
    body = {"name": name, "range": f"[{lo},{hi}]", "chunk_size": chunk,
            "workers": [{"worker_id": ids[n]} for n in (workers or WORKERS)]}
    code, job = http("POST", f"{API}/api/jobs", body)
    assert code == 201, (code, job)
    http("POST", f"{API}/api/jobs/{job['id']}/start")
    return job["id"]


def job_state(jid):
    try:
        code, j = http("GET", f"{API}/api/jobs/{jid}", timeout=5)
        return j if code == 200 else None
    except Exception:  # noqa: BLE001 — координатор может быть недоступен
        return None


def wait_job(jid, timeout=240, on_tick=None, terminal=("completed", "failed", "stopped")):
    start = time.time()
    while time.time() - start < timeout:
        j = job_state(jid)
        if on_tick:
            on_tick(time.time() - start, j)
        if j and j["status"] in terminal:
            return j
        time.sleep(1)
    raise TimeoutError(f"задание {jid} не завершилось за {timeout} с")


def all_items(jid):
    items, offset = [], 0
    while True:
        page = None
        for _ in range(30):  # после перезапуска БД/координатора API может отвечать ошибкой ещё несколько секунд
            try:
                code, page = http("GET", f"{API}/api/jobs/{jid}/items?limit=1000&offset={offset}")
                if code == 200 and isinstance(page, dict):
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
        else:
            raise RuntimeError("API не отвечает при чтении результатов")
        items += page["items"]
        offset += 1000
        if offset >= page["total"]:
            return items


def truth(vid):
    """Что на самом деле отвечает mock для этого id."""
    code, meta = http("GET", f"{MOCK}/{vid}")
    return code, meta


def verify(jid, lo, hi, allow_failed=0):
    """Возвращает список найденных проблем (пустой — всё хорошо)."""
    problems = []
    mock_reset()
    items = {i["external_id"]: i for i in all_items(jid)}
    missing = [i for i in range(lo, hi + 1) if i not in items]
    if missing:
        problems.append(f"нет результата для {len(missing)} ID (первые: {missing[:5]})")
    expected_sha, failed = {}, 0
    for vid in range(lo, hi + 1):
        it = items.get(vid)
        if not it:
            continue
        code, meta = truth(vid)
        want = {200: "completed", 404: "not_found", 403: "forbidden"}[code]
        if code == 200:
            expected_sha[vid] = meta["sha256"]
        if it["status"] == "failed":
            failed += 1
        elif it["status"] != want:
            problems.append(f"id {vid}: статус {it['status']}, ожидался {want}")
    if failed > allow_failed:
        problems.append(f"неуспешных элементов: {failed} (допустимо {allow_failed})")

    # целостность файлов на серверах: {prefix}/{id}/video.* (+ metadata.json)
    found_files, parts = {}, []
    for w in WORKERS:
        out = dc("exec", "-T", w, "sh", "-c",
                 "cd /data/videos && sha256sum */*/video.* 2>/dev/null; ls */*/*.part 2>/dev/null | sed 's/^/PART /'", check=False).stdout
        for line in out.splitlines():
            if line.startswith("PART "):
                parts.append((int(line[5:].split("/")[1]), line[5:], w))
                continue
            digest, _, name = line.partition("  ")
            if name.endswith(".part"):
                continue
            vid = int(name.split("/")[1])
            if lo <= vid <= hi:
                found_files.setdefault(vid, []).append((w, digest))
    # .part считается проблемой, только если готового файла нет ни на одном сервере:
    # после убийства агента его недокачанные файлы остаются на диске, а блок доделывает другой сервер
    for vid, name, w in parts:
        if lo <= vid <= hi and vid not in found_files:
            problems.append(f"остался недокачанный файл {name} на {w}, готового файла нет нигде")
    for vid, sha in expected_sha.items():
        have = found_files.get(vid)
        if items.get(vid, {}).get("status") == "failed":
            continue
        if not have:
            problems.append(f"id {vid}: файла нет ни на одном сервере")
        elif any(d != sha for _, d in have):
            problems.append(f"id {vid}: контрольная сумма не совпала на {[w for w, d in have if d != sha]}")
    # записи о файлах в БД совпадают с диском
    db_files, offset = {}, 0
    while True:  # страницы по 1000: файлов может быть больше
        code, page = http("GET", f"{API}/api/jobs/{jid}/files?limit=1000&offset={offset}")
        if code != 200:
            break
        db_files.update({f["external_id"]: f for f in page["files"]})
        offset += 1000
        if offset >= page["total"]:
            break
    for vid, sha in expected_sha.items():
        rec = db_files.get(vid)
        if items.get(vid, {}).get("status") == "completed":
            if not rec:
                problems.append(f"id {vid}: нет записи о файле в БД")
            elif rec["checksum_sha256"] != sha:
                problems.append(f"id {vid}: контрольная сумма в БД не совпала с метаданными")
    if len(db_files) != sum(1 for i in items.values() if i["status"] == "completed"):
        problems.append(f"записей о файлах в БД: {len(db_files)}, завершённых элементов: {sum(1 for i in items.values() if i['status'] == 'completed')}")
    return problems, {"ids": hi - lo + 1, "files": len(expected_sha), "failed": failed}


# ------------------------------------------------------------------ сценарии


def baseline():
    lo, hi = 1000, 1199
    jid = create_job("chaos-baseline", lo, hi)
    j = wait_job(jid, 120)
    return jid, lo, hi, 0, [] if j["status"] == "completed" else [f"статус {j['status']}"]


def flaky_mock():
    lo, hi = 2000, 2399
    mock_config(server_error_permille=150, drop_permille=150, delay_ms=20)
    jid = create_job("chaos-flaky-mock", lo, hi)
    fired = []

    def tick(t, j):
        if t > 5 and not fired:
            http("POST", f"{MOCK}/_control/rate_limit", {"seconds": 8, "retry_after_seconds": 2})
            fired.append(t)

    j = wait_job(jid, 300, tick)
    return jid, lo, hi, 2, [] if j["status"] == "completed" else [f"статус {j['status']}"]


def kill_agent():
    lo, hi = 3000, 3799
    mock_config(chunk_delay_ms=250, delay_ms=30)  # задание должно идти заметно дольше, чем до срабатывания таймаута
    jid = create_job("chaos-kill-agent", lo, hi)
    ids = worker_ids()
    state = {"killed": False, "offline_seen": False}
    problems = []

    def tick(t, j):
        if not state["killed"] and t > 3:
            # убиваем агента только когда он действительно держит блок, иначе сценарий ничего не проверяет
            chunks = http("GET", f"{API}/api/jobs/{jid}/chunks?limit=200")[1] or []
            if any(c["status"] == "leased" and c["leased_by"] == ids["worker-02"] for c in chunks):
                dc("exec", "-T", "worker-02", "pkill", "-9", "-f", "videofetch_agent", check=False)
                state["killed"] = True
        if state["killed"]:
            st = {w["name"]: w["status"] for w in http("GET", f"{API}/api/workers")[1]}
            if st.get("worker-02") == "offline":
                state["offline_seen"] = True

    j = wait_job(jid, 300, tick)
    if not state["killed"]:
        problems.append("агент не был убит: worker-02 ни разу не держал блок")
    if j["status"] != "completed":
        problems.append(f"статус {j['status']}")
    if state["killed"] and not state["offline_seen"]:
        problems.append("координатор не пометил worker-02 как offline")
    chunks = http("GET", f"{API}/api/jobs/{jid}/chunks?limit=200")[1]
    if state["killed"] and not any(c["attempt"] > 1 for c in chunks):
        problems.append("ни один блок не был выдан повторно (ожидалась перераздача блока убитого агента)")
    http("POST", f"{API}/api/workers/{ids['worker-02']}/agent/start", timeout=60)  # вернуть агента в строй
    return jid, lo, hi, 0, problems


def coordinator_restart():
    lo, hi = 4000, 4399
    mock_config(chunk_delay_ms=60, delay_ms=20)
    jid = create_job("chaos-coordinator-restart", lo, hi)
    fired = []

    def tick(t, j):
        if t > 6 and not fired:
            fired.append(t)
            dc("restart", "coordinator")

    j = wait_job(jid, 300, tick)
    wait_healthy()
    return jid, lo, hi, 0, [] if j["status"] == "completed" else [f"статус {j['status']}"]


def postgres_restart():
    lo, hi = 5000, 5599
    mock_config(chunk_delay_ms=400, delay_ms=40)  # задание идёт ~15–30 с: перезапуск БД точно попадает в середину
    jid = create_job("chaos-postgres-restart", lo, hi)
    fired = []

    def tick(t, j):
        # перезапускаем БД, когда задание уже идёт и ещё не закончилось
        if not fired and j and j["status"] == "running" and 50 <= j["stats"]["processed_ids"] < (hi - lo + 1) - 150:
            fired.append(t)
            dc("restart", "postgres")

    j = wait_job(jid, 300, tick)
    problems = [] if j["status"] == "completed" else [f"статус {j['status']}"]
    if not fired:
        problems.append("Postgres не был перезапущен: задание успело завершиться раньше")
    return jid, lo, hi, 0, problems


def _alerts():
    try:
        code, data = http("GET", f"{API}/api/alerts", timeout=5)
        return data if code == 200 and isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def rate_limit_429():
    """Целевой сервис отвечает 429: агенты обязаны снизить частоту запросов, а задание — завершиться."""
    lo, hi = 7000, 7399
    mock_config(chunk_delay_ms=150, delay_ms=40)
    jid = create_job("chaos-rate-limit", lo, hi)
    seen = {"fired": False, "alert": False, "min_scale": 1.0}

    def tick(t, j):
        if not seen["fired"] and j and j["status"] == "running" and j["stats"]["processed_ids"] >= 40:
            http("POST", f"{MOCK}/_control/rate_limit", {"seconds": 15, "retry_after_seconds": 2})
            seen["fired"] = True
        if seen["fired"]:
            if any(a.get("code") == "rate_limited" for a in _alerts()):
                seen["alert"] = True
            for w in http("GET", f"{API}/api/workers")[1] or []:
                scale = ((w.get("resources") or {}).get("runtime") or {}).get("throttle_scale")
                if isinstance(scale, (int, float)):
                    seen["min_scale"] = min(seen["min_scale"], scale)

    j = wait_job(jid, 300, tick)
    problems = [] if j["status"] == "completed" else [f"статус {j['status']}"]
    if not seen["fired"]:
        problems.append("ограничение 429 не было включено: задание завершилось слишком быстро")
    elif not seen["alert"] and seen["min_scale"] >= 1.0:
        problems.append("ответы 429 не вызвали ни предупреждения, ни снижения нагрузки агентами")
    return jid, lo, hi, 2, problems


def disk_full():
    """На сервере не хватает места: он перестаёт получать блоки, остальные серверы доделывают задание."""
    lo, hi = 8000, 8399
    mock_config(chunk_delay_ms=120, delay_ms=30)
    ids = worker_ids()
    w3 = ids["worker-03"]
    http("PATCH", f"{API}/api/workers/{w3}", {"min_free_space_gb": 10_000_000})  # «свободно меньше резерва»
    seen = {"alert": False}
    try:
        jid = create_job("chaos-disk-full", lo, hi)

        def tick(t, j):
            if any(a.get("code") == "disk_low" and a.get("worker_id") == w3 for a in _alerts()):
                seen["alert"] = True

        j = wait_job(jid, 300, tick)
        problems = [] if j["status"] == "completed" else [f"статус {j['status']}"]
        if not seen["alert"]:
            problems.append("нет предупреждения disk_low для worker-03")
        by_w3 = [i for i in all_items(jid) if i.get("worker_id") == w3]
        if by_w3:
            problems.append(f"worker-03 получил блоки при нехватке места: обработал {len(by_w3)} ID")
    finally:
        http("PATCH", f"{API}/api/workers/{w3}", {"min_free_space_gb": 0})
    return jid, lo, hi, 0, problems


def corrupt_part():
    """Перед заданием на серверах лежат повреждённые .part: итоговые файлы обязаны быть корректными."""
    lo, hi = 9000, 9399
    found = []
    for vid in range(lo, hi + 1):
        if http("GET", f"{MOCK}/{vid}")[0] == 200:
            found.append(vid)
        if len(found) == 4:
            break
    for vid in found:
        for w in WORKERS:  # какой бы сервер ни взял ID, на нём уже лежит мусор вместо начала файла
            dc("exec", "-T", "-u", "downloader", w, "sh", "-c",
               f"mkdir -p /data/videos/0/{vid} && head -c 40000 /dev/urandom > /data/videos/0/{vid}/video.mp4.part", check=False)
    mock_config(chunk_delay_ms=60, delay_ms=20)
    jid = create_job("chaos-corrupt-part", lo, hi)
    j = wait_job(jid, 300)
    problems = [] if j["status"] == "completed" else [f"статус {j['status']}"]
    if not found:
        problems.append("в диапазоне нет найденных материалов — сценарий ничего не проверяет")
    return jid, lo, hi, 0, problems


def network_drop():
    """Обрыв сети у сервера: аренда истекает, блок уходит другому, после возврата сети сервер снова в работе."""
    lo, hi = 10000, 10599
    mock_config(chunk_delay_ms=250, delay_ms=30)
    jid = create_job("chaos-network-drop", lo, hi)
    ids = worker_ids()
    cid = dc("ps", "-q", "worker-03").stdout.strip()
    nets = subprocess.run(["docker", "inspect", "-f", "{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}", cid],
                          capture_output=True, text=True, check=True).stdout.split()
    state = {"cut": False, "offline_seen": False, "back": False}
    problems = []

    def tick(t, j):
        if not state["cut"] and t > 3:
            chunks = http("GET", f"{API}/api/jobs/{jid}/chunks?limit=200")[1] or []
            if any(c["status"] == "leased" and c["leased_by"] == ids["worker-03"] for c in chunks):
                for n in nets:
                    subprocess.run(["docker", "network", "disconnect", n, cid], capture_output=True)
                state["cut"] = True
        if state["cut"] and not state["offline_seen"]:
            st = {w["name"]: w["status"] for w in http("GET", f"{API}/api/workers")[1]}
            if st.get("worker-03") == "offline":
                state["offline_seen"] = True
                for n in nets:  # возвращаем сеть вместе с DNS-именем сервиса
                    subprocess.run(["docker", "network", "connect", "--alias", "worker-03", n, cid], capture_output=True)
                state["back"] = True

    try:
        j = wait_job(jid, 300, tick)
    finally:
        if state["cut"] and not state["back"]:
            for n in nets:
                subprocess.run(["docker", "network", "connect", "--alias", "worker-03", n, cid], capture_output=True)
    if not state["cut"]:
        problems.append("сеть не была отключена: worker-03 ни разу не держал блок")
    if j["status"] != "completed":
        problems.append(f"статус {j['status']}")
    if state["cut"] and not state["offline_seen"]:
        problems.append("координатор не пометил worker-03 как offline")
    chunks = http("GET", f"{API}/api/jobs/{jid}/chunks?limit=200")[1] or []
    if state["cut"] and not any(c["attempt"] > 1 for c in chunks):
        problems.append("ни один блок не был выдан повторно")
    end = time.time() + 40  # агент должен вернуться в строй после восстановления сети
    while time.time() < end:
        if {w["name"]: w["status"] for w in http("GET", f"{API}/api/workers")[1]}.get("worker-03") == "online":
            break
        time.sleep(1)
    else:
        problems.append("worker-03 не вернулся в online после восстановления сети")
    return jid, lo, hi, 0, problems


def stop_resume():
    lo, hi = 6000, 6999
    mock_config(chunk_delay_ms=250, delay_ms=30)  # задание должно идти достаточно долго, чтобы успеть поставить на паузу
    jid = create_job("chaos-pause-resume", lo, hi)
    problems = []
    # пауза, как только появился прогресс (а не через фиксированное время: скорость стенда разная)
    end = time.time() + 60
    while time.time() < end and job_state(jid)["stats"]["processed_ids"] < 50:
        time.sleep(0.5)
    http("POST", f"{API}/api/jobs/{jid}/pause")
    j = wait_job(jid, 120, terminal=("paused", "completed", "failed", "stopped"))
    if j["status"] != "paused":
        problems.append(f"после паузы статус {j['status']}")
    done_at_pause = j["stats"]["processed_ids"]
    time.sleep(8)
    j2 = job_state(jid)
    if j2["stats"]["processed_ids"] != done_at_pause:
        problems.append("на паузе прогресс продолжал расти")
    http("POST", f"{API}/api/jobs/{jid}/resume")
    j = wait_job(jid, 240)
    if j["status"] != "completed":
        problems.append(f"статус {j['status']}")
    return jid, lo, hi, 0, problems


SCENARIOS = {f.__name__: f for f in (baseline, flaky_mock, kill_agent, coordinator_restart, postgres_restart, stop_resume,
                                       rate_limit_429, disk_full, corrupt_part, network_drop)}


def main(argv):
    names = argv or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print("неизвестные сценарии:", unknown, "доступны:", list(SCENARIOS))
        return 2
    wait_healthy()
    ensure_agents()
    results = []
    for name in names:
        print(f"\n=== {name} ===", flush=True)
        t0 = time.time()
        mock_reset()
        try:
            jid, lo, hi, allow_failed, problems = SCENARIOS[name]()
            if not problems or all("статус" not in p for p in problems):
                vp, stats = verify(jid, lo, hi, allow_failed)
                problems += vp
            else:
                stats = {}
        except Exception as exc:  # noqa: BLE001
            problems, stats = [f"исключение: {exc!r}"], {}
        finally:
            mock_reset()
            try:
                wait_healthy(60)
            except RuntimeError:
                pass
        ok = not problems
        results.append((name, ok, time.time() - t0, stats, problems))
        print(("PASS" if ok else "FAIL"), f"({time.time() - t0:.0f} с)", stats)
        for p in problems[:10]:
            print("  -", p)
    print("\n=== Итог ===")
    for name, ok, dur, stats, _ in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:22s} {dur:5.0f} с  {stats}")
    return 0 if all(r[1] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
