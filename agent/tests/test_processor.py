import threading

import pytest

from videofetch_agent.client import LeaseLost
from videofetch_agent.processor import ChunkProcessor, Counters
from videofetch_agent.util import RateGate


class StubClient:
    """Заглушка координатора: запоминает всё, что агент отправил."""

    def __init__(self, job_status="running", lose_after=None):
        self.progress_calls, self.complete_calls = [], []
        self.job_status, self.lose_after = job_status, lose_after
        self.threads = 4

    def progress(self, chunk_id, next_id, items):
        if self.lose_after is not None and len(self.progress_calls) >= self.lose_after:
            raise LeaseLost("lease_lost")
        self.progress_calls.append((next_id, list(items)))
        return {"job_status": self.job_status, "lease_expires_at": "x", "settings": {
            "url_template": None, "threads": self.threads, "rate_limit_rps": None,
            "request_timeout_seconds": 5, "max_retries": 2, "storage_path": self.storage}}

    def complete(self, chunk_id, items):
        self.complete_calls.append(list(items))
        return {"job_status": "running"}


def make_chunk(target, tmp_path, start, end, threads=4, next_id=None):
    return {"chunk_id": 1, "job_id": 1, "range_start": start, "range_end": end, "next_id": next_id if next_id is not None else start,
            "attempt": 1, "settings": {"url_template": target.template, "threads": threads, "rate_limit_rps": None,
                                       "request_timeout_seconds": 5, "max_retries": 2, "storage_path": str(tmp_path / "v")}}


def run(target, tmp_path, client, **kw):
    client.storage = str(tmp_path / "v")
    proc = ChunkProcessor(client, RateGate(), Counters(), report_every=3, report_interval=60)
    chunk = make_chunk(target, tmp_path, kw.pop("start", 0), kw.pop("end", 9), **kw)
    return proc, proc.process(chunk, threading.Event())


def all_items(client):
    seen = {}
    for _, items in client.progress_calls:
        for i in items:
            seen[i["external_id"]] = i
    for items in client.complete_calls:
        for i in items:
            seen[i["external_id"]] = i
    return seen


def test_whole_chunk_gets_a_result_per_id(target, tmp_path):
    client = StubClient()
    proc, result = run(target, tmp_path, client)
    assert result == "completed"
    items = all_items(client)
    assert sorted(items) == list(range(10))
    assert {i: items[i]["status"] for i in (1, 2, 3, 7)} == {1: "completed", 2: "completed", 3: "completed", 7: "completed"}
    assert items[5]["status"] == "forbidden" and items[0]["status"] == "not_found"
    assert proc.counters.snapshot()["ids"] == 10


def test_checkpoint_never_passes_an_unfinished_id(target, tmp_path):
    client = StubClient()
    run(target, tmp_path, client)
    reported = set()
    for next_id, items in client.progress_calls:
        reported |= {i["external_id"] for i in items}
        assert all(i in reported for i in range(next_id)), "контрольная точка обогнала результаты"
    assert client.progress_calls, "ожидались промежуточные отчёты"


def test_resume_starts_from_checkpoint(target, tmp_path):
    client = StubClient()
    _, result = run(target, tmp_path, client, next_id=6)
    assert result == "completed"
    assert sorted(all_items(client)) == [6, 7, 8, 9]


def test_lost_lease_abandons_chunk_without_complete(target, tmp_path):
    client = StubClient(lose_after=1)
    _, result = run(target, tmp_path, client, end=29)
    assert result == "abandoned" and client.complete_calls == []


def test_stopped_job_abandons_chunk(target, tmp_path):
    client = StubClient(job_status="stopped")
    _, result = run(target, tmp_path, client, end=29)
    assert result == "abandoned" and client.complete_calls == []


def test_pausing_job_lets_current_chunk_finish(target, tmp_path):
    client = StubClient(job_status="pausing")
    _, result = run(target, tmp_path, client)
    assert result == "completed"


def test_global_stop_abandons(target, tmp_path):
    client = StubClient()
    client.storage = str(tmp_path / "v")
    stop = threading.Event()
    stop.set()
    proc = ChunkProcessor(client, RateGate(), Counters())
    assert proc.process(make_chunk(target, tmp_path, 0, 9), stop) == "abandoned"
    assert stop.is_set()  # глобальный флаг не сброшен и не нужен блоку


def test_block_stop_does_not_leak_into_global_event(target, tmp_path):
    client = StubClient(job_status="stopped")
    client.storage = str(tmp_path / "v")
    stop = threading.Event()
    proc = ChunkProcessor(client, RateGate(), Counters(), report_every=2)
    proc.process(make_chunk(target, tmp_path, 0, 29), stop)
    assert not stop.is_set()
