"""Идемпотентное сохранение результатов проверки идентификаторов."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Download, FileRecord, Item, MediaMetadata, RangeChunk
from app.schemas import ItemIn
from app.services.errors import Invalid
from app.statuses import ItemStatus

BATCH = 1000


def _insert_for(session: Session):
    name = session.get_bind().dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError(f"Неподдерживаемая СУБД: {name}")
    return insert


def upsert_items(
    session: Session, chunk: RangeChunk, worker_id: int, results: list[ItemIn], now: dt.datetime
) -> int:
    """Сохраняет результаты блока. Повторная отправка не создаёт дубликатов.

    Уникальный ключ (job_id, external_id); завершённую загрузку (completed) повторное
    сообщение с более ранним статусом не перезаписывает.
    """
    if not results:
        return 0

    unique: dict[int, ItemIn] = {}
    for r in results:
        if not (chunk.range_start <= r.external_id <= chunk.range_end):
            raise Invalid(
                f"Идентификатор {r.external_id} вне блока {chunk.range_start}-{chunk.range_end}",
                "item_out_of_chunk",
            )
        unique[r.external_id] = r  # при повторе в одном запросе побеждает последнее сообщение

    rows = [
        {
            "job_id": chunk.job_id,
            "chunk_id": chunk.id,
            "external_id": r.external_id,
            "status": r.status.value,
            "http_status": r.http_status,
            "error_code": r.error_code,
            "worker_id": worker_id,
            "checked_at": now,
            "attempts": 1,
        }
        for r in unique.values()
    ]

    insert = _insert_for(session)
    done = ItemStatus.COMPLETED.value
    for i in range(0, len(rows), BATCH):
        stmt = insert(Item).values(rows[i : i + BATCH])
        stmt = stmt.on_conflict_do_update(
            index_elements=["job_id", "external_id"],
            set_={
                "chunk_id": stmt.excluded.chunk_id,
                "status": stmt.excluded.status,
                "http_status": stmt.excluded.http_status,
                "error_code": stmt.excluded.error_code,
                "worker_id": stmt.excluded.worker_id,
                "checked_at": stmt.excluded.checked_at,
                "attempts": Item.attempts + 1,
                "updated_at": func.now(),
            },
            where=or_(Item.status != done, stmt.excluded.status == done),
        )
        session.execute(stmt)
    _store_media_and_files(session, chunk, worker_id, list(unique.values()), now)
    return len(rows)


def _store_media_and_files(
    session: Session, chunk: RangeChunk, worker_id: int, results: list[ItemIn], now: dt.datetime
) -> None:
    """Метаданные материала (по одной записи на элемент) и завершённая загрузка с файлом. Идемпотентно."""
    with_data = [r for r in results if r.media or r.file]
    if not with_data:
        return
    ids = {
        ext: iid
        for iid, ext in session.execute(
            select(Item.id, Item.external_id).where(
                Item.job_id == chunk.job_id, Item.external_id.in_([r.external_id for r in with_data])
            )
        )
    }
    insert = _insert_for(session)

    for r in with_data:
        item_id = ids[r.external_id]
        if r.media:
            values = {"item_id": item_id, **r.media.model_dump()}
            stmt = insert(MediaMetadata).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=["item_id"],
                set_={k: getattr(stmt.excluded, k) for k in r.media.model_dump()},
            )
            session.execute(stmt)

        if r.file and r.status == ItemStatus.COMPLETED:
            download = session.scalar(
                select(Download).where(Download.item_id == item_id, Download.status == "completed")
            )
            if download is None:
                download = Download(
                    item_id=item_id,
                    worker_id=worker_id,
                    status="completed",
                    bytes_total=r.file.size_bytes,
                    bytes_done=r.file.size_bytes,
                    started_at=now,
                    finished_at=now,
                )
                session.add(download)
                session.flush()
            stmt = insert(FileRecord).values(
                download_id=download.id,
                storage_path=r.file.storage_path,
                size_bytes=r.file.size_bytes,
                mime_type=r.file.mime_type,
                checksum_sha256=r.file.checksum_sha256,
            )
            session.execute(stmt.on_conflict_do_nothing(index_elements=["download_id", "storage_path"]))
