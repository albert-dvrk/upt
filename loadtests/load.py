#!/usr/bin/env python3
"""Нагрузочный тест: 100+ параллельных операций на живом стенде.

Что создаёт нагрузку одновременно:
  * агенты: 3 сервера × N потоков проверяют/скачивают идентификаторы (по умолчанию 3 × 40 = 120 параллельных запросов к mock);
  * M «пользователей интерфейса» (по умолчанию 100): каждый со своей сессией непрерывно читает API
    (задания, блоки, элементы, файлы, события, серверы), как открытая панель управления.

Измеряется: задержка API (p50/p95/p99), доля ошибок, пропускная способность (ID/с), пик одновременных
операций на агентах (по метрикам heartbeat). В конце проверяется целостность результата (как в chaos.py).

Запуск из deploy/:   python3 ../loadtests/load.py [--ids 10000] [--threads 40] [--clients 100] [--delay-ms 50]
Результат дополнительно сохраняется в loadtests/results/load-<время>.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import random
import threading
import time
from pathlib import Path

import chaos
from vfclient import API, Session, http

LO = 20000


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * p / 100))]


def container_cpu() -> str:
    """Загрузка процессора контейнеров координатора и PostgreSQL (docker stats), чтобы видеть узкое место."""
    try:
        out = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.CPUPerc}}"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:  # noqa: BLE001
        return "?"
    keep = [ln.split() for ln in out.splitlines() if "coordinator" in ln or "postgres" in ln or "mock" in ln]
    return ", ".join(f"{n.replace('videofetch-', '').replace('-1', '')} {c}" for n, c in keep) or "?"


class ApiLoad:
    """M независимых клиентов с собственными сессиями."""

    def __init__(self, job_id: int, clients: int, worker_ids: list[int], think: float = 1.0) -> None:
        self.think = think
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.latency: dict[str, list[float]] = {}
        self.server: list[float] = []
        self.errors: list[str] = []
        self.count = 0
        self.job_id, self.worker_ids = job_id, worker_ids
        self.threads = [threading.Thread(target=self._run, args=(i,), daemon=True) for i in range(clients)]

    def endpoints(self):
        j = self.job_id
        return [
            ("jobs", "/api/jobs"), ("job", f"/api/jobs/{j}"), ("chunks", f"/api/jobs/{j}/chunks?limit=200"),
            ("items", f"/api/jobs/{j}/items?limit=100"), ("files", f"/api/jobs/{j}/files?limit=100"),
            ("events", f"/api/jobs/{j}/events?limit=100"), ("workers", "/api/workers"),
            ("metrics", f"/api/workers/{random.choice(self.worker_ids)}/metrics?limit=60"),
        ]

    def _run(self, n: int) -> None:
        s = Session()
        try:
            s.login()
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.errors.append(f"вход клиента {n}: {exc}")
            return
        while not self.stop.is_set():
            name, path = random.choice(self.endpoints())
            t0 = time.perf_counter()
            try:
                code, _ = s.request("GET", API + path, timeout=15)
                if s.server_ms is not None:
                    with self.lock:
                        self.server.append(float(s.server_ms) / 1000)
                err = None if code < 400 else f"{name}: HTTP {code}"
            except Exception as exc:  # noqa: BLE001
                err = f"{name}: {exc!r}"
            dt = time.perf_counter() - t0
            with self.lock:
                self.count += 1
                self.latency.setdefault(name, []).append(dt)
                if err:
                    self.errors.append(err)
            self.stop.wait(random.uniform(self.think * 0.5, self.think * 1.5))

    def start(self):
        for t in self.threads:
            t.start()

    def finish(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=20)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", type=int, default=10000)
    ap.add_argument("--threads", type=int, default=40, help="потоков на каждом из 3 серверов")
    ap.add_argument("--clients", type=int, default=100)
    ap.add_argument("--think", type=float, default=1.0, help="средняя пауза клиента между запросами, с (панель опрашивает раз в 2 с; 0.3 — стресс)")
    ap.add_argument("--chunk", type=int, default=500)
    ap.add_argument("--delay-ms", type=int, default=50, help="задержка ответа mock на каждый запрос")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args()

    chaos.wait_healthy()
    ids = chaos.ensure_agents()
    wids = [ids[n] for n in chaos.WORKERS]
    chaos.mock_reset()
    chaos.mock_config(delay_ms=a.delay_ms, chunk_delay_ms=0)

    hi = LO + a.ids - 1
    code, job = http("POST", f"{API}/api/jobs", {
        "name": "load-test", "range": f"[{LO},{hi}]", "chunk_size": a.chunk, "threads": a.threads,
        "workers": [{"worker_id": w} for w in wids]})
    assert code == 201, (code, job)
    jid = job["id"]
    print(f"задание {jid}: ID {LO}..{hi}, блок {a.chunk}, {a.threads} потоков × {len(wids)} сервера = {a.threads * len(wids)} параллельных операций; "
          f"{a.clients} клиентов API", flush=True)

    load = ApiLoad(jid, a.clients, wids, a.think)
    load.start()
    time.sleep(3)  # клиенты входят до старта задания, чтобы измерять работу под нагрузкой, а не вход
    t0 = time.time()
    http("POST", f"{API}/api/jobs/{jid}/start")

    peak_ops, samples, last_print = 0, [], 0.0

    def tick(t, j):
        nonlocal peak_ops, last_print
        total = 0
        for w in wids:
            code, rows = http("GET", f"{API}/api/workers/{w}/metrics?limit=1", timeout=10)
            if code == 200 and rows:
                total += (rows[0].get("active_checks") or 0) + (rows[0].get("active_downloads") or 0)
        peak_ops = max(peak_ops, total)
        samples.append(total)
        if j and t - last_print >= 10:
            last_print = t
            print(f"  {t:4.0f} с: обработано {j['stats']['processed_ids']}/{a.ids}, операций на агентах сейчас {total}, "
                  f"запросов API {load.count}; CPU: {container_cpu()}", flush=True)

    try:
        j = chaos.wait_job(jid, a.timeout, tick)
    finally:
        elapsed = time.time() - t0
        load.finish()

    problems = []
    if j["status"] != "completed":
        problems.append(f"статус задания {j['status']}")
    vp, stats = chaos.verify(jid, LO, hi, 0)
    problems += vp

    all_lat = [x for v in load.latency.values() for x in v]
    err_rate = len(load.errors) / max(1, load.count)
    res = {
        "ids": a.ids, "threads_per_worker": a.threads, "workers": len(wids), "parallel_operations": a.threads * len(wids),
        "api_clients": a.clients, "client_think_s": a.think, "duration_s": round(elapsed, 1), "ids_per_s": round(a.ids / elapsed, 1),
        "peak_agent_operations": peak_ops, "api_requests": load.count, "api_errors": len(load.errors),
        "api_error_rate": round(err_rate, 4),
        "api_latency_ms": {"p50": round(percentile(all_lat, 50) * 1000), "p95": round(percentile(all_lat, 95) * 1000),
                           "p99": round(percentile(all_lat, 99) * 1000), "max": round(max(all_lat or [0]) * 1000)},
        "server_time_ms": {"p50": round(percentile(load.server, 50) * 1000), "p95": round(percentile(load.server, 95) * 1000)},
        "per_endpoint_p95_ms": {k: round(percentile(v, 95) * 1000) for k, v in sorted(load.latency.items())},
        "integrity": stats, "problems": problems,
    }
    if load.errors:
        res["error_samples"] = sorted(set(load.errors))[:10]
    if err_rate > 0.01:
        problems.append(f"доля ошибок API {err_rate:.1%} (допустимо ≤ 1%)")
    if res["api_latency_ms"]["p95"] > 1500:
        problems.append(f"p95 задержки API {res['api_latency_ms']['p95']} мс (допустимо ≤ 1500)")

    print("\n=== Результат ===")
    print(f"длительность      {res['duration_s']} с, {res['ids_per_s']} ID/с")
    print(f"параллельно       заказано {res['parallel_operations']}, пик на агентах {peak_ops}")
    print(f"запросов API      {load.count}, ошибок {len(load.errors)} ({err_rate:.2%})")
    print(f"задержка API, мс  {res['api_latency_ms']}")
    print(f"на сервере, мс    {res['server_time_ms']}  (разница с задержкой клиента — очередь до приложения: TLS, event loop)")
    print(f"по эндпоинтам p95 {res['per_endpoint_p95_ms']}")
    print(f"целостность       {stats}")
    for p in problems:
        print("  -", p)
    out = Path(__file__).parent / "results"
    out.mkdir(exist_ok=True)
    path = out / f"load-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"сохранено: {path}")
    chaos.mock_reset()
    print("PASS" if not problems else "FAIL")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
