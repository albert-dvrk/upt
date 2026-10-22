"""Идемпотентное сохранение результатов проверки идентификаторов."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import Item, RangeChunk
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
    return len(rows)
