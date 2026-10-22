#!/usr/bin/env python3
"""Сквозная проверка стенда: 3 сервера → развёртывание агентов → задание [0,299] → ожидание завершения.

Запуск (после docker compose up -d --build):  python3 ../loadtests/smoke.py
"""

import sys
import time

from vfclient import API, http


def must(code_data, what):
    code, data = code_data
    if code >= 400:
        sys.exit(f"{what}: {code} {data}")
    return data


def main() -> int:
    existing = {w["name"]: w["id"] for w in must(http("GET", f"{API}/api/workers"), "список серверов")}
    ids = []
    for n in ("01", "02", "03"):
        name = f"worker-{n}"
        if name not in existing:
            existing[name] = must(http("POST", f"{API}/api/workers", {
                "name": name, "host": name, "ssh_user": "downloader", "ssh_key_ref": "/run/secrets/worker-key",
                "storage_path": "/data/videos", "threads": 8, "min_free_space_gb": 0}), f"создание {name}")["id"]
        wid = existing[name]
        ids.append(wid)
        must(http("POST", f"{API}/api/workers/{wid}/agent/deploy", timeout=60), f"deploy {name}")
        state = must(http("POST", f"{API}/api/workers/{wid}/agent/start", timeout=60), f"start {name}")["state"]
        print(f"{name} id={wid}: агент {state}")

    job = must(http("POST", f"{API}/api/jobs", {"name": "smoke", "range": "[0,299]", "chunk_size": 50,
                                                 "workers": [{"worker_id": i} for i in ids]}), "создание задания")["id"]
    must(http("POST", f"{API}/api/jobs/{job}/start"), "запуск задания")
    print(f"задание {job} запущено")
    for _ in range(120):
        d = must(http("GET", f"{API}/api/jobs/{job}"), "состояние")
        print(d["status"], d["stats"]["processed_ids"], d["stats"]["items"])
        if d["status"] == "completed":
            print("Готово. Файлы: docker compose exec worker-01 ls /data/videos")
            return 0
        time.sleep(2)
    print("Не завершилось за 4 минуты", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
