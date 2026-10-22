"""Сквозные проверки HTTP API (SQLite in-memory)."""

WORKER = {"name": "w1", "host": "10.0.0.1", "ssh_user": "downloader", "storage_path": "/data/videos"}


def items(start, end):
    return [{"external_id": i, "status": "not_found", "http_status": 404} for i in range(start, end + 1)]


def setup_job(client, range_="[0,19]", **extra):
    wid = client.post("/api/workers", json=WORKER).json()["id"]
    client.post("/api/workers/register", json={"name": "w1", "agent_version": "0.1.0"})
    body = {"name": "demo", "range": range_, "chunk_size": 10, "workers": [{"worker_id": wid}], **extra}
    r = client.post("/api/jobs", json=body)
    assert r.status_code == 201, r.text
    return wid, r.json()["id"]


def test_version_and_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}
    assert client.get("/api/version").json() == {"version": "0.1.0", "api": 1}


def test_full_flow(client):
    r = client.post("/api/workers", json={**WORKER, "ssh_key_ref": "/run/secrets/worker-key"})
    assert r.status_code == 201
    wid = r.json()["id"]
    assert "ssh_key_ref" not in r.json()  # ссылка на секрет в ответах не отдаётся

    r = client.post("/api/workers/register", json={"name": "w1", "agent_version": "0.1.0", "resources": {"disk_free_bytes": 10**12}})
    assert r.status_code == 200 and r.json()["worker_id"] == wid
    assert client.get(f"/api/workers/{wid}").json()["status"] == "online"

    r = client.post("/api/jobs", json={"name": "demo", "range": "[0,19]", "chunk_size": 10, "workers": [{"worker_id": wid}]})
    assert r.status_code == 201
    job = r.json()
    jid = job["id"]
    assert job["status"] == "created"
    assert client.post(f"/api/jobs/{jid}/start").json()["status"] == "running"

    lease = client.post(f"/api/workers/{wid}/lease").json()
    chunk = lease["chunk"]
    assert (chunk["range_start"], chunk["range_end"]) == (0, 9)
    assert chunk["settings"]["url_template"] == "http://mock:8080/{id}"
    assert chunk["settings"]["threads"] == 4

    r = client.post(f"/api/workers/{wid}/progress", json={"chunk_id": chunk["chunk_id"], "next_id": 5, "items": items(0, 4)})
    assert r.status_code == 200 and r.json()["job_status"] == "running"

    r = client.post(f"/api/workers/{wid}/complete", json={"chunk_id": chunk["chunk_id"], "items": items(0, 9)})
    assert r.status_code == 200 and r.json()["job_status"] == "running"

    chunk = client.post(f"/api/workers/{wid}/lease").json()["chunk"]
    assert (chunk["range_start"], chunk["range_end"]) == (10, 19)
    r = client.post(f"/api/workers/{wid}/complete", json={"chunk_id": chunk["chunk_id"], "items": items(10, 19)})
    assert r.json()["job_status"] == "completed"

    detail = client.get(f"/api/jobs/{jid}").json()
    assert detail["status"] == "completed"
    assert detail["stats"]["processed_ids"] == 20 and detail["stats"]["items"] == {"not_found": 20}
    assert detail["workers"][0]["name"] == "w1"

    page = client.get(f"/api/jobs/{jid}/items", params={"status": "not_found", "limit": 5}).json()
    assert page["total"] == 20 and len(page["items"]) == 5 and page["items"][0]["external_id"] == 0

    chunks = client.get(f"/api/jobs/{jid}/chunks").json()
    assert [c["status"] for c in chunks] == ["completed", "completed"]

    events = client.get(f"/api/jobs/{jid}/events").json()
    assert any(e["operation"] == "job_status" and e["result"] == "completed" for e in events)


def test_no_work_reason(client):
    wid, jid = setup_job(client)
    assert client.post(f"/api/workers/{wid}/lease").json() == {"chunk": None, "reason": "no_active_jobs"}


def test_pause_resume_stop_over_http(client):
    wid, jid = setup_job(client, "[0,]")
    client.post(f"/api/jobs/{jid}/start")
    assert client.post(f"/api/jobs/{jid}/pause").json()["status"] == "paused"
    assert client.post(f"/api/jobs/{jid}/resume").json()["status"] == "running"
    assert client.post(f"/api/jobs/{jid}/stop").json()["status"] == "stopped"
    r = client.post(f"/api/jobs/{jid}/start")
    assert r.status_code == 409 and r.json()["error"] == "invalid_transition"


def test_change_threads_without_stopping(client):
    wid, jid = setup_job(client, "[0,]")
    client.post(f"/api/jobs/{jid}/start")
    r = client.patch(f"/api/jobs/{jid}", json={"threads": 16})
    assert r.status_code == 200 and r.json()["config"]["threads"] == 16
    r = client.put(f"/api/jobs/{jid}/workers/{wid}", json={"threads": 32})
    assert r.status_code == 200 and r.json()["threads"] == 32
    beat = client.post(f"/api/workers/{wid}/heartbeat", json={"metrics": {"cpu_percent": 12.5, "ids_per_sec": 3}}).json()
    assert beat["jobs"] == [{"job_id": jid, "status": "running", "threads": 32, "rate_limit_rps": None}]
    metrics = client.get(f"/api/workers/{wid}/metrics").json()
    assert metrics[0]["cpu_percent"] == 12.5


def test_heartbeat_without_body(client):
    wid, _ = setup_job(client)
    assert client.post(f"/api/workers/{wid}/heartbeat").status_code == 200


def test_error_format(client):
    r = client.get("/api/jobs/999")
    assert r.status_code == 404 and r.json()["error"] == "job_not_found"

    r = client.post("/api/jobs", json={"name": "x", "range": "[9,3]"})
    assert r.status_code == 422 and r.json()["error"] == "bad_range"

    r = client.post("/api/jobs", json={"name": "x", "url_template": "http://kinescope.example/{id}"})
    assert r.status_code == 422 and r.json()["error"] == "target_not_allowed"

    jid = client.post("/api/jobs", json={"name": "x"}).json()["id"]
    r = client.post(f"/api/jobs/{jid}/start")
    assert r.status_code == 409 and r.json()["error"] == "no_workers"

    r = client.post("/api/workers/register", json={"name": "ghost", "agent_version": "1"})
    assert r.status_code == 404 and r.json()["error"] == "worker_unknown"

    client.post("/api/workers", json=WORKER)
    r = client.post("/api/workers", json=WORKER)
    assert r.status_code == 409 and r.json()["error"] == "worker_exists"


def test_input_validation(client):
    bad_path = {**WORKER, "name": "w2", "storage_path": "relative/path"}
    assert client.post("/api/workers", json=bad_path).status_code == 422
    traversal = {**WORKER, "name": "w3", "storage_path": "/data/../etc"}
    assert client.post("/api/workers", json=traversal).status_code == 422
    assert client.post("/api/workers", json={**WORKER, "name": "bad name!"}).status_code == 422
    assert client.post("/api/jobs", json={"name": "", "range": "[0,1]"}).status_code == 422


def test_lease_lost_after_stop(client):
    wid, jid = setup_job(client, "[0,]")
    client.post(f"/api/jobs/{jid}/start")
    chunk = client.post(f"/api/workers/{wid}/lease").json()["chunk"]
    client.post(f"/api/jobs/{jid}/stop")
    r = client.post(f"/api/workers/{wid}/progress", json={"chunk_id": chunk["chunk_id"], "next_id": 1, "items": items(0, 0)})
    assert r.status_code == 409 and r.json()["error"] == "lease_lost"


def test_incomplete_complete_is_rejected_and_not_applied(client):
    wid, jid = setup_job(client)
    client.post(f"/api/jobs/{jid}/start")
    chunk = client.post(f"/api/workers/{wid}/lease").json()["chunk"]
    r = client.post(f"/api/workers/{wid}/complete", json={"chunk_id": chunk["chunk_id"], "items": items(0, 4)})
    assert r.status_code == 409 and r.json()["error"] == "chunk_incomplete"
    # транзакция откатилась: результаты не сохранены, блок остаётся арендованным
    assert client.get(f"/api/jobs/{jid}/items").json()["total"] == 0
    assert client.get(f"/api/jobs/{jid}/chunks").json()[0]["status"] == "leased"


def test_fail_endpoint_returns_block_to_queue(client):
    wid, jid = setup_job(client)
    client.post(f"/api/jobs/{jid}/start")
    chunk = client.post(f"/api/workers/{wid}/lease").json()["chunk"]
    r = client.post(f"/api/workers/{wid}/fail", json={"chunk_id": chunk["chunk_id"], "error_code": "http_5xx", "message": "сбой"})
    assert r.status_code == 200 and r.json() == {"chunk_status": "pending", "job_status": "running"}
    again = client.post(f"/api/workers/{wid}/lease").json()["chunk"]
    assert again["chunk_id"] == chunk["chunk_id"] and again["attempt"] == 2


def test_patch_worker_disable(client):
    wid, jid = setup_job(client, "[0,]")
    client.post(f"/api/jobs/{jid}/start")
    client.post(f"/api/workers/{wid}/lease")
    r = client.patch(f"/api/workers/{wid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    assert client.get(f"/api/jobs/{jid}/chunks").json()[0]["status"] == "pending"
    r = client.post(f"/api/workers/{wid}/lease")
    assert r.status_code == 409 and r.json()["error"] == "worker_disabled"
