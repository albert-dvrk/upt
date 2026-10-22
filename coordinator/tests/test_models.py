"""Проверка ограничений схемы (уникальность, CHECK, каскады) на SQLite in-memory.

Поведение на PostgreSQL (SKIP LOCKED, ON CONFLICT) проверяется интеграционными тестами недели 2.
"""

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Base, Download, FileRecord, Item, Job, RangeChunk, Worker


@pytest.fixture
def session():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_job(s, **kw):
    job = Job(name="t", range_from=0, range_to=999, **kw)
    s.add(job)
    s.flush()
    return job


def test_all_spec_tables_exist():
    expected = {
        "users", "roles", "user_roles", "workers", "worker_metrics", "jobs", "job_workers",
        "range_chunks", "items", "media_metadata", "downloads", "files", "events", "audit_log",
    }
    assert expected == set(Base.metadata.tables)


def test_job_defaults(session):
    job = make_job(session)
    session.commit()
    assert job.status == "created" and job.chunk_size == 1000 and job.attempt == 1


def test_job_range_check(session):
    session.add(Job(name="bad", range_from=10, range_to=5))
    with pytest.raises(IntegrityError):
        session.flush()


def test_open_range_allowed(session):
    session.add(Job(name="inf", range_from=0, range_to=None))
    session.flush()


def test_duplicate_chunk_rejected(session):
    job = make_job(session)
    session.add(RangeChunk(job_id=job.id, range_start=0, range_end=99, next_id=0))
    session.flush()
    session.add(RangeChunk(job_id=job.id, range_start=0, range_end=99, next_id=0))
    with pytest.raises(IntegrityError):
        session.flush()


def test_chunk_next_id_must_stay_in_bounds(session):
    job = make_job(session)
    session.add(RangeChunk(job_id=job.id, range_start=10, range_end=19, next_id=5))
    with pytest.raises(IntegrityError):
        session.flush()


def test_chunk_checkpoint_after_last_id_is_allowed(session):
    job = make_job(session)
    session.add(RangeChunk(job_id=job.id, range_start=10, range_end=19, next_id=20))
    session.flush()


def test_duplicate_item_rejected(session):
    job = make_job(session)
    session.add(Item(job_id=job.id, external_id=42))
    session.flush()
    session.add(Item(job_id=job.id, external_id=42))
    with pytest.raises(IntegrityError):
        session.flush()


def test_same_external_id_in_other_job_is_fine(session):
    j1, j2 = make_job(session), make_job(session)
    session.add_all([Item(job_id=j1.id, external_id=42), Item(job_id=j2.id, external_id=42)])
    session.flush()


def test_duplicate_file_path_for_download_rejected(session):
    job = make_job(session)
    item = Item(job_id=job.id, external_id=1)
    session.add(item)
    session.flush()
    dl = Download(item_id=item.id)
    session.add(dl)
    session.flush()
    kw = dict(download_id=dl.id, storage_path="/data/videos/0/1/video.mp4", size_bytes=1, checksum_sha256="0" * 64)
    session.add(FileRecord(**kw))
    session.flush()
    session.add(FileRecord(**kw))
    with pytest.raises(IntegrityError):
        session.flush()


def test_worker_unique_name_and_threads_check(session):
    session.add(Worker(name="w1", host="h", ssh_user="u", storage_path="/d"))
    session.flush()
    session.add(Worker(name="w1", host="h2", ssh_user="u", storage_path="/d"))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
    session.add(Worker(name="w2", host="h", ssh_user="u", storage_path="/d", threads=0))
    with pytest.raises(IntegrityError):
        session.flush()


def test_deleting_job_cascades_to_chunks_and_items(session):
    job = make_job(session)
    session.add_all(
        [RangeChunk(job_id=job.id, range_start=0, range_end=9, next_id=0), Item(job_id=job.id, external_id=3)]
    )
    session.flush()
    session.delete(job)
    session.flush()
    assert session.scalars(select(RangeChunk)).all() == []
    assert session.scalars(select(Item)).all() == []
