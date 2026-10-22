import pytest
from sqlalchemy import select

from app.models import Item, JobWorker, RangeChunk
from app.schemas import JobCreate, WorkerCreate
from app.services import jobs, leases, workers
from app.services.errors import Conflict, Invalid, NotFound
from helpers import T0, finish, lease, make_job, make_worker, results_for


@pytest.mark.parametrize(
    "text, expected",
    [("[5,10]", (5, 10)), ("7", (0, 7)), ("[3,]", (3, None)), ("", (0, None)), (None, (0, None))],
)
def test_create_job_accepts_all_range_formats(session, text, expected):
    job = jobs.create_job(session, JobCreate(name="j", range=text))
    assert (job.range_from, job.range_to) == expected
    assert job.status == "created"


def test_create_job_with_from_to(session):
    job = jobs.create_job(session, JobCreate(name="j", range_from=4, range_to=9))
    assert (job.range_from, job.range_to) == (4, 9)
    job = jobs.create_job(session, JobCreate(name="j"))
    assert (job.range_from, job.range_to) == (0, None)


@pytest.mark.parametrize("bad", ["[9,3]", "abc", "-1", "[,5]"])
def test_create_job_rejects_bad_ranges(session, bad):
    with pytest.raises(Invalid) as exc:
        jobs.create_job(session, JobCreate(name="j", range=bad))
    assert exc.value.code == "bad_range"


def test_range_and_from_to_cannot_be_mixed(session):
    with pytest.raises(Invalid):
        jobs.create_job(session, JobCreate(name="j", range="[0,5]", range_from=1))


def test_only_allowed_target_hosts(session):
    with pytest.raises(Invalid) as exc:
        jobs.create_job(session, JobCreate(name="j", url_template="http://evil.example/{id}"))
    assert exc.value.code == "target_not_allowed"
    with pytest.raises(Invalid):
        jobs.create_job(session, JobCreate(name="j", url_template="http://mock:8080/video"))  # нет {id}
    with pytest.raises(Invalid):
        jobs.create_job(session, JobCreate(name="j", url_template="ftp://mock/{id}"))
    ok = jobs.create_job(session, JobCreate(name="j", url_template="http://localhost:9000/{id}"))
    assert ok.config["url_template"] == "http://localhost:9000/{id}"


def test_unknown_worker_in_job_is_rejected(session):
    with pytest.raises(NotFound):
        jobs.create_job(session, JobCreate(name="j", workers=[{"worker_id": 999}]))


def test_start_requires_a_worker(session):
    job = make_job(session, [], start=False)
    with pytest.raises(Conflict) as exc:
        jobs.start_job(session, job.id)
    assert exc.value.code == "no_workers"


def test_start_moves_job_to_running(session):
    w = make_worker(session)
    job = make_job(session, [w.id], start=False)
    jobs.start_job(session, job.id, now=T0)
    session.commit()
    assert job.status == "running" and job.started_at is not None


def test_completed_job_cannot_be_started_again(session):
    w = make_worker(session)
    job = make_job(session, [w.id], range="[0,9]")
    finish(session, w, lease(session, w)["chunk"])
    session.refresh(job)
    assert job.status == "completed"
    with pytest.raises(Conflict) as exc:
        jobs.start_job(session, job.id)
    assert exc.value.code == "invalid_transition"


def test_pause_without_active_blocks_is_immediate(session):
    w = make_worker(session)
    job = make_job(session, [w.id])
    jobs.pause_job(session, job.id, now=T0)
    session.commit()
    assert job.status == "paused"
    assert lease(session, w) == {"chunk": None, "reason": "no_active_jobs"}
    jobs.resume_job(session, job.id, now=T0)
    session.commit()
    assert job.status == "running"
    assert lease(session, w)["chunk"] is not None


def test_soft_pause_waits_for_the_running_block(session):
    w, w2 = make_worker(session, "w1"), make_worker(session, "w2")
    job = make_job(session, [w.id, w2.id])
    chunk = lease(session, w)["chunk"]

    jobs.pause_job(session, job.id, now=T0)
    session.commit()
    session.refresh(job)
    assert job.status == "pausing"
    assert lease(session, w2)["chunk"] is None  # новые блоки не выдаются

    with pytest.raises(Conflict) as exc:
        jobs.resume_job(session, job.id)
    assert exc.value.code == "pausing_in_progress"

    finish(session, w, chunk)  # текущий блок можно дозавершить
    session.refresh(job)
    assert job.status == "paused"


def test_stop_releases_blocks_and_cuts_off_the_old_owner(session):
    w = make_worker(session)
    job = make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    jobs.stop_job(session, job.id, now=T0)
    session.commit()
    session.refresh(job)
    assert job.status == "stopped" and job.finished_at is not None

    chunk = session.get(RangeChunk, c["chunk_id"])
    session.refresh(chunk)
    assert chunk.status == "pending" and chunk.leased_by is None
    with pytest.raises(Conflict) as exc:
        leases.report_progress(session, w.id, c["chunk_id"], 1, [])
    assert exc.value.code == "lease_lost"
    with pytest.raises(Conflict):
        jobs.resume_job(session, job.id)  # из stopped вернуться нельзя


def test_stop_keeps_the_checkpoint(session):
    w = make_worker(session)
    job = make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    leases.report_progress(session, w.id, c["chunk_id"], 7, results_for(0, 6), now=T0)
    session.commit()
    jobs.stop_job(session, job.id, now=T0)
    session.commit()
    chunk = session.get(RangeChunk, c["chunk_id"])
    session.refresh(chunk)
    assert chunk.next_id == 7
    assert len(session.scalars(select(Item)).all()) == 7


def test_created_job_can_be_cancelled(session):
    job = make_job(session, [], start=False)
    jobs.stop_job(session, job.id)
    assert job.status == "stopped"


def test_threads_priority_individual_over_global_over_server(session):
    w = make_worker(session, threads=4)
    job = make_job(session, [w.id], threads=8)
    jw = session.get(JobWorker, (job.id, w.id))
    assert jobs.effective_threads(job, jw, w) == 8  # глобальное значение задания

    jobs.set_job_worker(session, job, w.id, {"threads": 16})
    assert jobs.effective_threads(job, jw, w) == 16  # индивидуальное имеет приоритет

    jobs.set_job_worker(session, job, w.id, {"threads": None})
    jobs.update_job(session, job.id, {"threads": None})
    assert jobs.effective_threads(job, jw, w) == 4  # остаётся значение сервера


def test_threads_can_be_changed_while_running_and_reach_the_agent(session):
    w = make_worker(session, threads=4)
    job = make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    assert c["settings"]["threads"] == 4

    jobs.update_job(session, job.id, {"threads": 12})
    session.commit()
    beat = workers.heartbeat(session, w.id, now=T0)
    assert beat["jobs"][0]["threads"] == 12
    prog = leases.report_progress(session, w.id, c["chunk_id"], 1, results_for(0, 0), now=T0)
    assert prog["settings"]["threads"] == 12


def test_update_job_rejects_null_for_required_fields(session):
    job = make_job(session, [], start=False)
    with pytest.raises(Invalid):
        jobs.update_job(session, job.id, {"max_retries": None})


def test_global_rate_limit_is_split_between_online_workers(session):
    w1, w2 = make_worker(session, "w1"), make_worker(session, "w2")
    job = make_job(session, [w1.id, w2.id], global_rate_limit_rps=20)
    workers.heartbeat(session, w1.id, now=T0)
    workers.heartbeat(session, w2.id, now=T0)
    session.commit()
    jw = session.get(JobWorker, (job.id, w1.id))
    assert jobs.effective_rate_limit(session, job, jw) == 10

    jobs.set_job_worker(session, job, w1.id, {"rate_limit_rps": 4})
    assert jobs.effective_rate_limit(session, job, jw) == 4  # индивидуальный лимит ниже доли


def test_disabling_a_worker_in_a_job_releases_its_block(session):
    w = make_worker(session)
    job = make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    jobs.set_job_worker(session, job, w.id, {"enabled": False})
    session.commit()
    assert session.get(RangeChunk, c["chunk_id"]).status == "pending"
    assert lease(session, w) == {"chunk": None, "reason": "no_active_jobs"}


def test_job_stats(session):
    w = make_worker(session)
    job = make_job(session, [w.id], range="[0,99]")
    finish(session, w, lease(session, w)["chunk"])
    c = lease(session, w)["chunk"]
    leases.report_progress(session, w.id, c["chunk_id"], 14, results_for(10, 13), now=T0)
    session.commit()

    stats = jobs.job_stats(session, job)
    assert stats["total_ids"] == 100
    assert stats["processed_ids"] == 14  # 10 завершённых + 4 в текущем блоке
    assert stats["chunks"] == {"completed": 1, "leased": 1}
    assert stats["items"] == {"not_found": 14}
    assert stats["progress"] == pytest.approx(0.14)


def test_worker_registration_and_duplicates(session):
    make_worker(session, "w1")
    with pytest.raises(Conflict) as exc:
        workers.create_worker(
            session, WorkerCreate(name="w1", host="h", ssh_user="u", storage_path="/data/videos")
        )
    assert exc.value.code == "worker_exists"

    reg = workers.register_agent(session, "w1", "0.1.0", {"cpu_count": 4}, now=T0)
    assert reg["lease_seconds"] == 60 and reg["storage_path"] == "/data/videos"
    with pytest.raises(NotFound) as exc:
        workers.register_agent(session, "nope", "0.1.0", None)
    assert exc.value.code == "worker_unknown"


def test_disabled_worker_is_cut_off(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    workers.update_worker(session, w.id, {"enabled": False})
    session.commit()
    session.refresh(w)
    assert w.status == "disabled"
    assert session.get(RangeChunk, c["chunk_id"]).status == "pending"
    with pytest.raises(Conflict) as exc:
        leases.lease_chunk(session, w.id, now=T0)
    assert exc.value.code == "worker_disabled"
    with pytest.raises(Conflict):
        workers.heartbeat(session, w.id, now=T0)
