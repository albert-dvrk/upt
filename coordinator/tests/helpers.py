"""Вспомогательные функции тестов."""

import datetime as dt

from app.schemas import ItemIn, JobCreate, WorkerCreate
from app.services import jobs, leases, workers
from app.statuses import ItemStatus

T0 = dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


def secs(n: int) -> dt.timedelta:
    return dt.timedelta(seconds=n)


def make_worker(session, name="w1", **kw):
    data = WorkerCreate(name=name, host="10.0.0.1", ssh_user="downloader", storage_path="/data/videos", **kw)
    worker = workers.create_worker(session, data)
    session.commit()
    return worker


def make_job(session, worker_ids=(), *, range="[0,99]", chunk_size=10, start=True, **kw):
    data = JobCreate(
        name="job",
        range=range,
        chunk_size=chunk_size,
        workers=[{"worker_id": wid} for wid in worker_ids],
        **kw,
    )
    job = jobs.create_job(session, data)
    session.commit()
    if start:
        jobs.start_job(session, job.id, now=T0)
        session.commit()
    return job


def results_for(start, end, status=ItemStatus.NOT_FOUND):
    return [ItemIn(external_id=i, status=status, http_status=404) for i in range(start, end + 1)]


def lease(session, worker, now=T0):
    result = leases.lease_chunk(session, worker.id, now=now)
    session.commit()
    return result


def finish(session, worker, chunk, now=T0):
    """Завершает блок, отправив результат для каждого идентификатора."""
    out = leases.complete_chunk(
        session, worker.id, chunk["chunk_id"], results_for(chunk["range_start"], chunk["range_end"]), now=now
    )
    session.commit()
    return out
