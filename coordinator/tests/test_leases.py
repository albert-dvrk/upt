import pytest
from sqlalchemy import func, select

from app.models import Item, RangeChunk
from app.schemas import ItemIn
from app.services import leases, workers
from app.services.errors import Conflict, Invalid
from app.statuses import ItemStatus
from app.timeutil import as_utc
from helpers import T0, finish, lease, make_job, make_worker, results_for, secs


def test_workers_get_contiguous_blocks_without_overlaps(session):
    w1, w2 = make_worker(session, "w1"), make_worker(session, "w2")
    make_job(session, [w1.id, w2.id], range="[0,]", chunk_size=10)
    seen = []
    for _ in range(3):
        for w in (w1, w2):
            chunk = lease(session, w)["chunk"]
            seen.append((chunk["range_start"], chunk["range_end"]))
            finish(session, w, chunk)
    assert seen == [(0, 9), (10, 19), (20, 29), (30, 39), (40, 49), (50, 59)]


def test_repeated_lease_request_returns_the_same_chunk(session):
    w = make_worker(session)
    make_job(session, [w.id])
    first = lease(session, w)["chunk"]
    again = lease(session, w, now=T0 + secs(5))["chunk"]
    assert again["chunk_id"] == first["chunk_id"]
    assert again["attempt"] == 1


def test_no_blocks_for_unstarted_job_or_foreign_worker(session):
    w1, w2 = make_worker(session, "w1"), make_worker(session, "w2")
    make_job(session, [w1.id], start=False)
    assert lease(session, w1) == {"chunk": None, "reason": "no_active_jobs"}
    make_job(session, [w1.id])
    assert lease(session, w2) == {"chunk": None, "reason": "no_active_jobs"}  # w2 не участвует в заданиях


def test_finite_range_is_split_exactly_and_job_completes(session):
    w = make_worker(session)
    job = make_job(session, [w.id], range="[0,24]", chunk_size=10)
    got = []
    while (chunk := lease(session, w)["chunk"]) is not None:
        got.append((chunk["range_start"], chunk["range_end"]))
        finish(session, w, chunk)
    assert got == [(0, 9), (10, 19), (20, 24)]
    session.refresh(job)
    assert job.status == "completed" and job.finished_at is not None


def test_max_ids_caps_open_range(session):
    w = make_worker(session)
    job = make_job(session, [w.id], range="[0,]", chunk_size=100, max_ids=250)
    got = []
    while (chunk := lease(session, w)["chunk"]) is not None:
        got.append((chunk["range_start"], chunk["range_end"]))
        finish(session, w, chunk)
    assert got == [(0, 99), (100, 199), (200, 249)]
    session.refresh(job)
    assert job.status == "completed"


def test_open_range_never_runs_out(session):
    w = make_worker(session)
    job = make_job(session, [w.id], range="", chunk_size=1000)
    for i in range(5):
        chunk = lease(session, w)["chunk"]
        assert chunk["range_start"] == i * 1000
        finish(session, w, chunk)
    session.refresh(job)
    assert job.status == "running"


def test_expired_lease_is_reclaimed_by_another_worker(session):
    w1, w2 = make_worker(session, "w1"), make_worker(session, "w2")
    make_job(session, [w1.id, w2.id])
    first = lease(session, w1)["chunk"]
    second = lease(session, w2, now=T0 + secs(61))["chunk"]
    assert second["chunk_id"] == first["chunk_id"]
    assert second["attempt"] == 2
    with pytest.raises(Conflict) as exc:
        leases.report_progress(session, w1.id, first["chunk_id"], 5, [], now=T0 + secs(61))
    assert exc.value.code == "lease_lost"


def test_progress_extends_lease_and_checkpoint_only_moves_forward(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]

    res = leases.report_progress(session, w.id, c["chunk_id"], 5, results_for(0, 4), now=T0 + secs(10))
    session.commit()
    assert as_utc(res["lease_expires_at"]) == T0 + secs(70)
    assert res["job_status"] == "running"

    leases.report_progress(session, w.id, c["chunk_id"], 3, [], now=T0 + secs(11))
    session.commit()
    assert session.get(RangeChunk, c["chunk_id"]).next_id == 5


def test_progress_validates_bounds_and_item_ids(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    with pytest.raises(Invalid) as exc:
        leases.report_progress(session, w.id, c["chunk_id"], 11, [])
    assert exc.value.code == "bad_next_id"
    with pytest.raises(Invalid) as exc:
        leases.report_progress(session, w.id, c["chunk_id"], 5, results_for(0, 12))
    assert exc.value.code == "item_out_of_chunk"


def test_only_owner_can_report(session):
    w1, w2 = make_worker(session, "w1"), make_worker(session, "w2")
    make_job(session, [w1.id, w2.id])
    c = lease(session, w1)["chunk"]
    with pytest.raises(Conflict) as exc:
        leases.report_progress(session, w2.id, c["chunk_id"], 1, [])
    assert exc.value.code == "lease_lost"


def test_resending_items_creates_no_duplicates(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    for _ in range(3):
        leases.report_progress(session, w.id, c["chunk_id"], 5, results_for(0, 4))
        session.commit()
    assert session.scalar(select(func.count()).select_from(Item)) == 5


def test_completed_item_is_not_overwritten_by_a_late_message(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    leases.report_progress(session, w.id, c["chunk_id"], 4, [ItemIn(external_id=3, status=ItemStatus.COMPLETED)])
    leases.report_progress(session, w.id, c["chunk_id"], 4, [ItemIn(external_id=3, status=ItemStatus.FOUND)])
    session.commit()
    assert session.scalar(select(Item.status).where(Item.external_id == 3)) == "completed"


def test_complete_requires_a_result_for_every_id(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    with pytest.raises(Conflict) as exc:
        leases.complete_chunk(session, w.id, c["chunk_id"], results_for(0, 4), now=T0)
    assert exc.value.code == "chunk_incomplete"
    session.rollback()
    assert session.get(RangeChunk, c["chunk_id"]).status == "leased"


def test_duplicate_complete_message_is_not_an_error(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    finish(session, w, c)
    again = finish(session, w, c)
    assert again["job_status"] == "running"


def test_failed_chunk_goes_back_to_queue_until_attempts_run_out(session):
    w = make_worker(session)
    job = make_job(session, [w.id], range="[0,9]")
    for attempt in range(1, 6):
        c = lease(session, w)["chunk"]
        assert c["attempt"] == attempt
        leases.fail_chunk(session, w.id, c["chunk_id"], "boom", "тест", True, now=T0)
        session.commit()
    assert session.scalars(select(RangeChunk)).one().status == "failed"
    session.refresh(job)
    assert job.status == "completed"  # все блоки в терминальном состоянии; ошибки видны в статистике


def test_non_retryable_failure_is_final(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    out = leases.fail_chunk(session, w.id, c["chunk_id"], "bad_config", None, False, now=T0)
    assert out["chunk_status"] == "failed"


def test_low_disk_space_pauses_new_leases(session):
    w = make_worker(session, min_free_space_gb=20)
    make_job(session, [w.id])
    workers.heartbeat(session, w.id, resources={"disk_free_bytes": 1024**3}, now=T0)
    session.commit()
    assert lease(session, w) == {"chunk": None, "reason": "disk_low"}
    workers.heartbeat(session, w.id, resources={"disk_free_bytes": 100 * 1024**3}, now=T0 + secs(10))
    session.commit()
    assert lease(session, w, now=T0 + secs(10))["chunk"] is not None


def test_heartbeat_extends_leases(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    workers.heartbeat(session, w.id, now=T0 + secs(50))
    session.commit()
    chunk = session.get(RangeChunk, c["chunk_id"])
    assert as_utc(chunk.lease_expires_at) == T0 + secs(110)


def test_reaper_returns_blocks_of_a_silent_worker(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    stats = leases.reap_expired(session, now=T0 + secs(31))
    session.commit()
    assert stats["workers_offline"] == 1 and stats["chunks_released"] == 1
    session.refresh(w)
    assert w.status == "offline"
    chunk = session.get(RangeChunk, c["chunk_id"])
    assert chunk.status == "pending" and chunk.leased_by is None


def test_reaper_releases_expired_leases_of_live_workers(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    workers.heartbeat(session, w.id, now=T0 + secs(100))  # сервер жив, но аренду продлили только в heartbeat
    session.commit()
    chunk = session.get(RangeChunk, c["chunk_id"])
    chunk.lease_expires_at = T0 + secs(60)  # имитация истёкшей аренды
    session.commit()
    stats = leases.reap_expired(session, now=T0 + secs(101))
    session.commit()
    assert stats["chunks_released"] == 1
    assert session.get(RangeChunk, c["chunk_id"]).status == "pending"


def test_reaper_stops_job_by_time_limit(session):
    w = make_worker(session)
    job = make_job(session, [w.id], max_runtime_seconds=100)
    lease(session, w)
    stats = leases.reap_expired(session, now=T0 + secs(101))
    session.commit()
    assert stats["jobs_stopped"] == 1
    session.refresh(job)
    assert job.status == "stopped"
