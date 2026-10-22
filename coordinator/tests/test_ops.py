"""Условие остановки, повтор ошибочных, алерты, активные загрузки, экспорт."""

import csv
import io
import json

import pytest
from sqlalchemy import func, select

from app.models import Item, WorkerMetric
from app.schemas import ItemIn, MetricsIn
from app.services import alerts, jobs, leases, workers
from app.services.errors import Conflict
from app.statuses import ItemStatus
from app.timeutil import utcnow
from helpers import T0, finish, lease, make_job, make_worker, results_for, secs

GIB = 1024 ** 3


def mixed(start, end, present=()):
    """Результаты блока: not_found, кроме перечисленных id (они «найдены и скачаны»)."""
    return [
        ItemIn(external_id=i, status=ItemStatus.COMPLETED, http_status=200) if i in present
        else ItemIn(external_id=i, status=ItemStatus.NOT_FOUND, http_status=404)
        for i in range(start, end + 1)
    ]


def complete(session, worker, chunk, present=()):
    out = leases.complete_chunk(session, worker.id, chunk["chunk_id"], mixed(chunk["range_start"], chunk["range_end"], present), now=T0)
    session.commit()
    return out


# ------------------------------------------------------------------ stop_on_consecutive_missing


def test_job_completes_after_n_consecutive_missing(session):
    w = make_worker(session)
    job = make_job(session, [w.id], range="[0,]", chunk_size=10, stop_on_consecutive_missing=25)
    complete(session, w, lease(session, w)["chunk"], present={3})      # frontier 9, пропусков подряд 6
    complete(session, w, lease(session, w)["chunk"])                   # frontier 19 → 16
    session.refresh(job)
    assert job.status == "running"
    out = complete(session, w, lease(session, w)["chunk"])             # frontier 29 → 26 ≥ 25
    assert out["job_status"] == "completed"
    assert lease(session, w) == {"chunk": None, "reason": "no_active_jobs"}


def test_found_item_resets_the_run(session):
    w = make_worker(session)
    job = make_job(session, [w.id], range="[0,]", chunk_size=10, stop_on_consecutive_missing=15)
    complete(session, w, lease(session, w)["chunk"])                   # 0..9 нет → 10
    complete(session, w, lease(session, w)["chunk"], present={19})     # найден 19 → 0
    complete(session, w, lease(session, w)["chunk"])                   # 20..29 нет → 10
    session.refresh(job)
    assert job.status == "running"
    complete(session, w, lease(session, w)["chunk"])                   # 30..39 → 20 ≥ 15
    session.refresh(job)
    assert job.status == "completed"


def test_errors_count_as_present_not_missing(session):
    w = make_worker(session)
    job = make_job(session, [w.id], range="[0,]", chunk_size=10, stop_on_consecutive_missing=5)
    c = lease(session, w)["chunk"]
    items = mixed(0, 9)
    items[9] = ItemIn(external_id=9, status=ItemStatus.FAILED, error_code="http_5xx")  # про 9 неизвестно, есть ли материал
    leases.complete_chunk(session, w.id, c["chunk_id"], items, now=T0)
    session.commit()
    session.refresh(job)
    assert job.status == "running"  # от 9 до границы 9 — ноль подряд отсутствующих


def test_open_earlier_block_blocks_the_stop(session):
    w1, w2 = make_worker(session, "w1"), make_worker(session, "w2")
    job = make_job(session, [w1.id, w2.id], range="[0,]", chunk_size=10, stop_on_consecutive_missing=15)
    first, second = lease(session, w1)["chunk"], lease(session, w2)["chunk"]
    complete(session, w2, second)  # 10..19 готов, но 0..9 ещё не обработан
    session.refresh(job)
    assert job.status == "running"
    complete(session, w1, first)   # теперь граница 19, подряд 20 ≥ 15
    session.refresh(job)
    assert job.status == "completed"


def test_stop_condition_cuts_off_other_workers(session):
    w1, w2 = make_worker(session, "w1"), make_worker(session, "w2")
    job = make_job(session, [w1.id, w2.id], range="[0,]", chunk_size=10, stop_on_consecutive_missing=10)
    first = lease(session, w1)["chunk"]   # 0..9
    busy = lease(session, w2)["chunk"]    # 10..19 — у второго сервера, он ещё работает
    complete(session, w1, first)          # граница 9, подряд 10 ≥ 10 → задание завершено
    session.refresh(job)
    assert job.status == "completed"
    with pytest.raises(Conflict) as exc:
        leases.report_progress(session, w2.id, busy["chunk_id"], 12, [])
    assert exc.value.code == "lease_lost"


def test_option_is_off_by_default(session):
    w = make_worker(session)
    job = make_job(session, [w.id], range="[0,]", chunk_size=10)
    for _ in range(5):
        complete(session, w, lease(session, w)["chunk"])
    session.refresh(job)
    assert job.status == "running" and job.stop_on_consecutive_missing is None


# ------------------------------------------------------------------ повтор ошибочных


def finish_with_failures(session, w, job_range="[0,29]", failed=(4, 25)):
    job = make_job(session, [w.id], range=job_range, chunk_size=10)
    while (c := lease(session, w)["chunk"]) is not None:
        items = [ItemIn(external_id=i, status=ItemStatus.FAILED, error_code="http_5xx") if i in failed
                 else ItemIn(external_id=i, status=ItemStatus.NOT_FOUND, http_status=404)
                 for i in range(c["range_start"], c["range_end"] + 1)]
        leases.complete_chunk(session, w.id, c["chunk_id"], items, now=T0)
        session.commit()
    session.refresh(job)
    return job


def test_retry_failed_creates_new_attempt_over_failed_range(session):
    w = make_worker(session)
    job = finish_with_failures(session, w)
    assert job.status == "completed"
    new = jobs.retry_failed(session, job.id)
    session.commit()
    assert (new.range_from, new.range_to, new.attempt, new.status) == (4, 25, 2, "running")
    assert new.config["retry_of"] == job.id and "повтор 2" in new.name
    view = jobs.job_workers_view(session, new)
    assert [v["worker_id"] for v in view] == [w.id]


def test_retry_requires_finished_job_and_failures(session):
    w = make_worker(session)
    running = make_job(session, [w.id], range="[0,]")
    with pytest.raises(Conflict) as exc:
        jobs.retry_failed(session, running.id)
    assert exc.value.code == "job_not_finished"
    jobs.stop_job(session, running.id)
    session.commit()
    with pytest.raises(Conflict) as exc:
        jobs.retry_failed(session, running.id)
    assert exc.value.code == "nothing_to_retry"


def test_retry_over_http_and_roles(client):
    r = client.post("/api/workers", json={"name": "w1", "host": "h", "ssh_user": "u", "storage_path": "/data"})
    wid = r.json()["id"]
    client.post("/api/workers/register", json={"name": "w1", "agent_version": "1"})
    jid = client.post("/api/jobs", json={"name": "j", "range": "[0,9]", "chunk_size": 10, "workers": [{"worker_id": wid}],
                                         "stop_on_consecutive_missing": 100}).json()["id"]
    assert client.get(f"/api/jobs/{jid}").json()["stop_on_consecutive_missing"] == 100
    client.post(f"/api/jobs/{jid}/start")
    chunk = client.post(f"/api/workers/{wid}/lease").json()["chunk"]
    batch = [{"external_id": i, "status": "failed" if i == 7 else "not_found", "http_status": 404} for i in range(10)]
    client.post(f"/api/workers/{wid}/complete", json={"chunk_id": chunk["chunk_id"], "items": batch})
    r = client.post(f"/api/jobs/{jid}/retry-failed")
    assert r.status_code == 201 and (r.json()["range_from"], r.json()["range_to"]) == (7, 7)
    assert client.post(f"/api/jobs/{r.json()['id']}/retry-failed").json()["error"] == "job_not_finished"


# ------------------------------------------------------------------ алерты


def codes(items):
    return {a["code"]: a for a in items}


def test_offline_server_is_critical_when_it_has_work(session):
    w = make_worker(session)
    make_job(session, [w.id])
    workers.register_agent(session, "w1", "1", None, now=T0)
    w.status = "offline"
    session.commit()
    a = codes(alerts.compute_alerts(session, now=T0 + secs(300)))
    assert a["worker_offline"]["severity"] == "critical" and a["worker_offline"]["worker_id"] == w.id
    idle = make_worker(session, "idle")
    idle.status = "offline"
    session.commit()
    sev = {x["worker_id"]: x["severity"] for x in alerts.compute_alerts(session, now=T0) if x["code"] == "worker_offline"}
    assert sev[idle.id] == "warning"


def test_disk_alerts(session):
    w = make_worker(session)  # минимум 20 ГБ
    workers.register_agent(session, "w1", "1", {"disk_free_bytes": 5 * GIB}, now=T0)
    session.commit()
    assert codes(alerts.compute_alerts(session, now=T0))["disk_low"]["severity"] == "critical"
    workers.heartbeat(session, w.id, resources={"disk_free_bytes": 25 * GIB}, now=T0)
    session.commit()
    assert "disk_low_soon" in codes(alerts.compute_alerts(session, now=T0))  # меньше 1,5 × минимума
    workers.heartbeat(session, w.id, resources={"disk_free_bytes": 200 * GIB}, now=T0)
    session.commit()
    assert not [a for a in alerts.compute_alerts(session, now=T0) if a["code"].startswith("disk")]


def test_stalled_job_and_error_rate(session):
    w = make_worker(session)
    job = make_job(session, [w.id], range="[0,]", chunk_size=100)
    workers.register_agent(session, "w1", "1", None, now=T0)
    c = lease(session, w)["chunk"]
    bad = [ItemIn(external_id=i, status=ItemStatus.FAILED, error_code="http_5xx") for i in range(12)]
    leases.report_progress(session, w.id, c["chunk_id"], 12, bad, now=T0)
    session.commit()
    now = utcnow()
    a = codes(alerts.compute_alerts(session, now=now))
    assert a["high_error_rate"]["job_id"] == job.id and "job_stalled" not in a
    a = codes(alerts.compute_alerts(session, now=now + secs(400)))
    assert "job_stalled" in a  # результатов давно нет, блок всё ещё арендован


def test_rate_limit_and_retries_alerts(session):
    w = make_worker(session)
    make_job(session, [w.id], range="[0,]", chunk_size=100)
    c = lease(session, w)["chunk"]
    leases.report_progress(session, w.id, c["chunk_id"], 1, [ItemIn(external_id=0, status=ItemStatus.RATE_LIMITED, http_status=429)], now=T0)
    session.commit()
    assert "rate_limited" in codes(alerts.compute_alerts(session, now=utcnow()))
    for _ in range(3):
        leases.fail_chunk(session, w.id, c["chunk_id"], "http_5xx", "x", True)
        session.commit()
        c = lease(session, w)["chunk"]
    assert "chunk_retries" in codes(alerts.compute_alerts(session, now=utcnow()))


def test_runtime_from_heartbeat_feeds_downloads_and_throttle(session):
    w = make_worker(session)
    workers.register_agent(session, "w1", "1", {"disk_free_bytes": 500 * GIB}, now=T0)
    runtime = {"throttle_scale": 0.5, "downloads": [{"external_id": 7, "job_id": 3, "total": 1000, "bytes": 250, "bps": 100.0}]}
    workers.heartbeat(session, w.id, resources={"disk_free_bytes": 500 * GIB}, metrics=MetricsIn(cpu_percent=1.0, runtime=runtime), now=T0)
    session.commit()
    assert session.scalar(select(func.count()).select_from(WorkerMetric)) == 1  # runtime в таблицу метрик не попал
    d = alerts.active_downloads(session, now=T0)
    assert d == [{"worker_id": w.id, "worker": "w1", "job_id": 3, "external_id": 7, "bytes": 250, "total": 1000, "bps": 100.0, "percent": 25.0}]
    assert alerts.active_downloads(session, now=T0 + secs(120)) == []  # агент замолчал — данные устарели
    assert "throttled" in codes(alerts.compute_alerts(session, now=T0))
    # следующий heartbeat без runtime не стирает ресурсы, а с ними — прежнее состояние до нового отчёта
    workers.heartbeat(session, w.id, resources={"disk_free_bytes": 400 * GIB}, now=T0)
    session.commit()


def test_alert_and_download_endpoints(client):
    assert client.get("/api/alerts").json() == []
    assert client.get("/api/downloads").json() == []


# ------------------------------------------------------------------ экспорт


def seeded_job(client):
    wid = client.post("/api/workers", json={"name": "w1", "host": "h", "ssh_user": "u", "storage_path": "/data"}).json()["id"]
    client.post("/api/workers/register", json={"name": "w1", "agent_version": "1"})
    jid = client.post("/api/jobs", json={"name": "j", "range": "[0,4]", "chunk_size": 5, "workers": [{"worker_id": wid}]}).json()["id"]
    client.post(f"/api/jobs/{jid}/start")
    chunk = client.post(f"/api/workers/{wid}/lease").json()["chunk"]
    sha = "a" * 64
    items = [
        {"external_id": 0, "status": "not_found", "http_status": 404},
        {"external_id": 1, "status": "completed", "http_status": 200,
         "media": {"title": "=HYPERLINK(\"http://evil\")", "page_url": "http://mock:8080/1", "mime_type": "video/mp4", "size_bytes": 5, "checksum_sha256": sha},
         "file": {"storage_path": "0/1/video.mp4", "size_bytes": 5, "mime_type": "video/mp4", "checksum_sha256": sha}},
        {"external_id": 2, "status": "failed", "http_status": 500, "error_code": "http_5xx"},
        {"external_id": 3, "status": "forbidden", "http_status": 403},
        {"external_id": 4, "status": "not_found", "http_status": 404},
    ]
    assert client.post(f"/api/workers/{wid}/complete", json={"chunk_id": chunk["chunk_id"], "items": items}).status_code == 200
    return jid


def test_csv_export(client):
    jid = seeded_job(client)
    r = client.get(f"/api/jobs/{jid}/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert f'job-{jid}.csv' in r.headers["content-disposition"]
    assert r.text.startswith("﻿")
    rows = list(csv.DictReader(io.StringIO(r.text.lstrip("﻿"))))
    assert [x["external_id"] for x in rows] == ["0", "1", "2", "3", "4"]
    done = rows[1]
    assert done["status"] == "completed" and done["storage_path"] == "0/1/video.mp4" and done["checksum_sha256"] == "a" * 64
    assert done["title"].startswith("'=")  # формула из названия обезврежена
    assert rows[2]["error_code"] == "http_5xx" and rows[2]["storage_path"] == ""


def test_json_export_and_filter(client):
    jid = seeded_job(client)
    data = client.get(f"/api/jobs/{jid}/export", params={"format": "json"}).json()
    assert len(data) == 5 and data[1]["title"] == '=HYPERLINK("http://evil")'  # в JSON данные без искажений
    only = client.get(f"/api/jobs/{jid}/export", params={"format": "json", "status": "failed"}).json()
    assert [x["external_id"] for x in only] == [2]


def test_export_errors(client):
    assert client.get("/api/jobs/999/export").status_code == 404
    jid = seeded_job(client)
    assert client.get(f"/api/jobs/{jid}/export", params={"format": "xml"}).status_code == 422
