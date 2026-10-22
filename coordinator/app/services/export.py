"""Выгрузка результатов задания в CSV и JSON (потоком, страницами)."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Download, FileRecord, Item, MediaMetadata
from app.services import jobs

COLUMNS = ["external_id", "status", "http_status", "error_code", "worker_id", "checked_at", "title", "page_url",
           "mime_type", "size_bytes", "checksum_sha256", "storage_path"]
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> Any:
    """Текст из внешнего источника не должен превращаться в формулу при открытии CSV в Excel."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def _statement(job_id: int, status: str | None):
    latest = (
        select(Download.item_id.label("item_id"), func.max(FileRecord.id).label("fid"))
        .join(FileRecord, FileRecord.download_id == Download.id)
        .group_by(Download.item_id)
        .subquery()
    )
    stmt = (
        select(Item, MediaMetadata, FileRecord)
        .outerjoin(MediaMetadata, MediaMetadata.item_id == Item.id)
        .outerjoin(latest, latest.c.item_id == Item.id)
        .outerjoin(FileRecord, FileRecord.id == latest.c.fid)
        .where(Item.job_id == job_id)
        .order_by(Item.external_id)
    )
    if status:
        stmt = stmt.where(Item.status == status)
    return stmt


def _row(item: Item, media: MediaMetadata | None, file: FileRecord | None) -> dict[str, Any]:
    return {
        "external_id": item.external_id, "status": item.status, "http_status": item.http_status,
        "error_code": item.error_code, "worker_id": item.worker_id,
        "checked_at": item.checked_at.isoformat() if item.checked_at else None,
        "title": media.title if media else None, "page_url": media.page_url if media else None,
        "mime_type": (file.mime_type if file else None) or (media.mime_type if media else None),
        "size_bytes": file.size_bytes if file else (media.size_bytes if media else None),
        "checksum_sha256": file.checksum_sha256 if file else (media.checksum_sha256 if media else None),
        "storage_path": file.storage_path if file else None,
    }


def stream_export(factory: sessionmaker, job_id: int, fmt: str, status: str | None) -> Iterator[str]:
    """Генератор для StreamingResponse. Своя сессия: зависимость запроса к этому моменту может быть закрыта."""
    with factory() as session:
        result = session.execute(_statement(job_id, status).execution_options(yield_per=1000))
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(COLUMNS)
            yield "﻿" + buf.getvalue()  # BOM: Excel корректно открывает UTF-8
            for item, media, file in result:
                buf.seek(0), buf.truncate()
                row = _row(item, media, file)
                writer.writerow([csv_safe(row[c]) for c in COLUMNS])
                yield buf.getvalue()
        else:
            yield "["
            first = True
            for item, media, file in result:
                yield ("" if first else ",") + json.dumps(_row(item, media, file), ensure_ascii=False)
                first = False
            yield "]"


def check_job(session: Session, job_id: int) -> None:
    jobs.get_job(session, job_id)  # 404 до начала потока
