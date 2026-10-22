"""Метаданные материалов, загрузки и файлы: идемпотентность и проверка путей."""

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models import Download, FileRecord, Item, MediaMetadata
from app.schemas import FileIn, ItemIn, MediaIn
from app.services import jobs, leases
from app.statuses import ItemStatus
from helpers import T0, lease, make_job, make_worker, results_for

SHA = "a" * 64


def found_item(vid, status=ItemStatus.COMPLETED, with_file=True):
    return ItemIn(
        external_id=vid,
        status=status,
        http_status=200,
        media=MediaIn(title="Видео", page_url=f"http://mock:8080/{vid}", download_url=f"http://mock:8080/files/{vid}/video.mp4",
                      mime_type="video/mp4", size_bytes=123, checksum_sha256=SHA),
        file=FileIn(storage_path=f"0/{vid}/video.mp4", size_bytes=123, mime_type="video/mp4", checksum_sha256=SHA) if with_file else None,
    )


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_media_download_and_file_are_saved(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    leases.report_progress(session, w.id, c["chunk_id"], 3, [found_item(1), *results_for(0, 0), *results_for(2, 2)], now=T0)
    session.commit()
    media = session.scalars(select(MediaMetadata)).one()
    assert media.title == "Видео" and media.checksum_sha256 == SHA and media.size_bytes == 123
    d = session.scalars(select(Download)).one()
    assert d.status == "completed" and d.bytes_done == 123 and d.worker_id == w.id
    f = session.scalars(select(FileRecord)).one()
    assert f.storage_path == "0/1/video.mp4" and f.checksum_sha256 == SHA


def test_resending_does_not_duplicate_records(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    for _ in range(3):
        leases.report_progress(session, w.id, c["chunk_id"], 2, [found_item(1)], now=T0)
        session.commit()
    assert (count(session, Item), count(session, MediaMetadata), count(session, Download), count(session, FileRecord)) == (1, 1, 1, 1)


def test_metadata_is_updated_not_duplicated(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    leases.report_progress(session, w.id, c["chunk_id"], 2, [found_item(1)], now=T0)
    changed = found_item(1)
    changed.media.title = "Новое название"
    leases.report_progress(session, w.id, c["chunk_id"], 2, [changed], now=T0)
    session.commit()
    assert session.scalars(select(MediaMetadata)).one().title == "Новое название"


def test_failed_download_keeps_metadata_without_file(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    leases.report_progress(session, w.id, c["chunk_id"], 2, [found_item(1, ItemStatus.FAILED, with_file=False)], now=T0)
    session.commit()
    assert count(session, MediaMetadata) == 1 and count(session, Download) == 0 and count(session, FileRecord) == 0


def test_file_is_recorded_only_for_completed_items(session):
    w = make_worker(session)
    make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    leases.report_progress(session, w.id, c["chunk_id"], 2, [found_item(1, ItemStatus.FAILED)], now=T0)
    session.commit()
    assert count(session, FileRecord) == 0


@pytest.mark.parametrize("path", ["/etc/passwd", "../x/video.mp4", "a/../b", "a//b", "a\\b", "./a", "", "a/\x00b"])
def test_unsafe_file_paths_are_rejected(path):
    with pytest.raises(ValidationError):
        FileIn(storage_path=path, size_bytes=1, checksum_sha256=SHA)


def test_bad_checksum_is_rejected():
    with pytest.raises(ValidationError):
        FileIn(storage_path="0/1/video.mp4", size_bytes=1, checksum_sha256="xyz")
    with pytest.raises(ValidationError):
        MediaIn(page_url="http://mock/1", checksum_sha256="G" * 64)


def test_files_listing_service(session):
    w = make_worker(session)
    job = make_job(session, [w.id])
    c = lease(session, w)["chunk"]
    leases.report_progress(session, w.id, c["chunk_id"], 4, [found_item(1), found_item(3)], now=T0)
    session.commit()
    total, files = jobs.list_files(session, job.id, limit=10, offset=0)
    assert total == 2 and [f["external_id"] for f in files] == [1, 3]
    assert files[0]["title"] == "Видео" and files[0]["storage_path"] == "0/1/video.mp4"
    total, files = jobs.list_files(session, job.id, limit=1, offset=1)
    assert total == 2 and [f["external_id"] for f in files] == [3]
