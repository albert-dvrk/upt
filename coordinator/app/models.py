"""Схема БД (раздел 17 задания). Время хранится в UTC (timestamptz).

Статусы лежат в строковых колонках (значения — см. statuses.py), чтобы миграции
не зависели от PostgreSQL ENUM. Секреты (SSH-ключи) в БД не хранятся — только
ссылка на секрет (ssh_key_ref).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

BigInt = BigInteger().with_variant(Integer, "sqlite")
Json = JSON().with_variant(JSONB(), "postgresql")

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _now() -> Any:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _now_onupdate() -> Any:
    return mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def _pk() -> Any:
    return mapped_column(BigInt, primary_key=True, autoincrement=True)


# ---------------------------------------------------------------- доступ


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = _pk()
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    created_at: Mapped[dt.datetime] = _now()


class Role(Base):
    """Роли: viewer, operator, administrator."""

    __tablename__ = "roles"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(32), unique=True)


class UserRole(Base):
    __tablename__ = "user_roles"

    user_id: Mapped[int] = mapped_column(BigInt, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[int] = mapped_column(BigInt, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)


# ---------------------------------------------------------------- серверы


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(64), unique=True)
    host: Mapped[str] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22, server_default="22")
    ssh_user: Mapped[str] = mapped_column(String(64))
    auth_method: Mapped[str] = mapped_column(String(16), default="key", server_default="key")
    ssh_key_ref: Mapped[Optional[str]] = mapped_column(String(255))  # ссылка на секрет, не сам ключ
    agent_token_hash: Mapped[Optional[str]] = mapped_column(String(64))  # SHA-256 токена агента; сам токен не хранится
    threads: Mapped[int] = mapped_column(Integer, default=4, server_default="4")
    storage_path: Mapped[str] = mapped_column(String(512))
    min_free_space_gb: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    status: Mapped[str] = mapped_column(String(16), default="offline", server_default="offline")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    agent_version: Mapped[Optional[str]] = mapped_column(String(32))
    last_heartbeat_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    resources: Mapped[Optional[dict]] = mapped_column(Json)
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = _now_onupdate()

    __table_args__ = (CheckConstraint("threads >= 1", name="threads_positive"),)


class WorkerMetric(Base):
    __tablename__ = "worker_metrics"

    id: Mapped[int] = _pk()
    worker_id: Mapped[int] = mapped_column(BigInt, ForeignKey("workers.id", ondelete="CASCADE"))
    collected_at: Mapped[dt.datetime] = _now()
    cpu_percent: Mapped[Optional[float]] = mapped_column(Float)
    load1: Mapped[Optional[float]] = mapped_column(Float)
    ram_total: Mapped[Optional[int]] = mapped_column(BigInt)
    ram_used: Mapped[Optional[int]] = mapped_column(BigInt)
    swap_used: Mapped[Optional[int]] = mapped_column(BigInt)
    disk_total: Mapped[Optional[int]] = mapped_column(BigInt)
    disk_free: Mapped[Optional[int]] = mapped_column(BigInt)
    net_rx_bps: Mapped[Optional[float]] = mapped_column(Float)
    net_tx_bps: Mapped[Optional[float]] = mapped_column(Float)
    disk_read_bps: Mapped[Optional[float]] = mapped_column(Float)
    disk_write_bps: Mapped[Optional[float]] = mapped_column(Float)
    open_files: Mapped[Optional[int]] = mapped_column(Integer)
    active_connections: Mapped[Optional[int]] = mapped_column(Integer)
    active_checks: Mapped[Optional[int]] = mapped_column(Integer)
    active_downloads: Mapped[Optional[int]] = mapped_column(Integer)
    ids_per_sec: Mapped[Optional[float]] = mapped_column(Float)
    download_bps: Mapped[Optional[float]] = mapped_column(Float)
    http_status_counts: Mapped[Optional[dict]] = mapped_column(Json)

    __table_args__ = (Index("ix_worker_metrics_worker_collected", "worker_id", "collected_at"),)


# ---------------------------------------------------------------- задания


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = _pk()
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="created", server_default="created")
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    range_from: Mapped[int] = mapped_column(BigInt, default=0, server_default="0")
    range_to: Mapped[Optional[int]] = mapped_column(BigInt)  # NULL — открытый диапазон
    chunk_size: Mapped[int] = mapped_column(Integer, default=1000, server_default="1000")
    request_timeout_seconds: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    max_retries: Mapped[int] = mapped_column(Integer, default=4, server_default="4")
    global_rate_limit_rps: Mapped[Optional[float]] = mapped_column(Float)
    stop_on_consecutive_missing: Mapped[Optional[int]] = mapped_column(BigInt)  # по умолчанию выключено
    max_ids: Mapped[Optional[int]] = mapped_column(BigInt)
    max_runtime_seconds: Mapped[Optional[int]] = mapped_column(BigInt)
    config: Mapped[Optional[dict]] = mapped_column(Json)
    created_by: Mapped[Optional[int]] = mapped_column(BigInt, ForeignKey("users.id", ondelete="SET NULL"))
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = _now_onupdate()

    __table_args__ = (
        CheckConstraint("range_from >= 0", name="range_from_nonneg"),
        CheckConstraint("range_to IS NULL OR range_from <= range_to", name="range_order"),
        CheckConstraint("chunk_size >= 1", name="chunk_size_positive"),
    )


class JobWorker(Base):
    """Какие серверы участвуют в задании и их индивидуальные настройки."""

    __tablename__ = "job_workers"

    job_id: Mapped[int] = mapped_column(BigInt, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    worker_id: Mapped[int] = mapped_column(BigInt, ForeignKey("workers.id", ondelete="CASCADE"), primary_key=True)
    threads: Mapped[Optional[int]] = mapped_column(Integer)  # приоритет над глобальным значением
    rate_limit_rps: Mapped[Optional[float]] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=true(), default=True)
    added_at: Mapped[dt.datetime] = _now()


class RangeChunk(Base):
    __tablename__ = "range_chunks"

    id: Mapped[int] = _pk()
    job_id: Mapped[int] = mapped_column(BigInt, ForeignKey("jobs.id", ondelete="CASCADE"))
    range_start: Mapped[int] = mapped_column(BigInt)
    range_end: Mapped[int] = mapped_column(BigInt)  # включительно
    next_id: Mapped[int] = mapped_column(BigInt)  # контрольная точка
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    leased_by: Mapped[Optional[int]] = mapped_column(BigInt, ForeignKey("workers.id", ondelete="SET NULL"))
    lease_expires_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = _now_onupdate()

    __table_args__ = (
        UniqueConstraint("job_id", "range_start", "range_end", name="uq_range_chunks_job_range"),
        CheckConstraint("range_start <= range_end", name="bounds_order"),
        CheckConstraint("next_id >= range_start AND next_id <= range_end + 1", name="next_id_in_bounds"),
        Index("ix_range_chunks_lease", "job_id", "status", "lease_expires_at"),
    )


# ---------------------------------------------------------------- результаты


class Item(Base):
    __tablename__ = "items"

    id: Mapped[int] = _pk()
    job_id: Mapped[int] = mapped_column(BigInt, ForeignKey("jobs.id", ondelete="CASCADE"))
    chunk_id: Mapped[Optional[int]] = mapped_column(BigInt, ForeignKey("range_chunks.id", ondelete="SET NULL"))
    external_id: Mapped[int] = mapped_column(BigInt)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    http_status: Mapped[Optional[int]] = mapped_column(Integer)
    error_code: Mapped[Optional[str]] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    worker_id: Mapped[Optional[int]] = mapped_column(BigInt, ForeignKey("workers.id", ondelete="SET NULL"))
    checked_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = _now()
    updated_at: Mapped[dt.datetime] = _now_onupdate()

    __table_args__ = (
        UniqueConstraint("job_id", "external_id", name="uq_items_job_external"),
        Index("ix_items_job_status", "job_id", "status"),
    )


class MediaMetadata(Base):
    __tablename__ = "media_metadata"

    id: Mapped[int] = _pk()
    item_id: Mapped[int] = mapped_column(BigInt, ForeignKey("items.id", ondelete="CASCADE"), unique=True)
    title: Mapped[Optional[str]] = mapped_column(Text)  # исходное название; в путях не используется
    page_url: Mapped[str] = mapped_column(String(1024))
    download_url: Mapped[Optional[str]] = mapped_column(String(2048))
    mime_type: Mapped[Optional[str]] = mapped_column(String(128))
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInt)
    checksum_sha256: Mapped[Optional[str]] = mapped_column(String(64))
    extra: Mapped[Optional[dict]] = mapped_column(Json)
    discovered_at: Mapped[dt.datetime] = _now()


class Download(Base):
    __tablename__ = "downloads"

    id: Mapped[int] = _pk()
    item_id: Mapped[int] = mapped_column(BigInt, ForeignKey("items.id", ondelete="CASCADE"))
    worker_id: Mapped[Optional[int]] = mapped_column(BigInt, ForeignKey("workers.id", ondelete="SET NULL"))
    attempt: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    status: Mapped[str] = mapped_column(String(24), default="queued", server_default="queued")
    bytes_total: Mapped[Optional[int]] = mapped_column(BigInt)
    bytes_done: Mapped[int] = mapped_column(BigInt, default=0, server_default="0")
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[Optional[str]] = mapped_column(String(64))
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (Index("ix_downloads_item", "item_id"),)


class FileRecord(Base):
    __tablename__ = "files"

    id: Mapped[int] = _pk()
    download_id: Mapped[int] = mapped_column(BigInt, ForeignKey("downloads.id", ondelete="CASCADE"))
    storage_path: Mapped[str] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(BigInt)
    mime_type: Mapped[Optional[str]] = mapped_column(String(128))
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    completed_at: Mapped[dt.datetime] = _now()

    __table_args__ = (UniqueConstraint("download_id", "storage_path", name="uq_files_download_path"),)


# ---------------------------------------------------------------- журналы


class Event(Base):
    """Структурированный журнал (раздел 15). Без внешних ключей: записи живут дольше сущностей."""

    __tablename__ = "events"

    id: Mapped[int] = _pk()
    ts: Mapped[dt.datetime] = _now()
    level: Mapped[str] = mapped_column(String(8))
    component: Mapped[str] = mapped_column(String(32))
    job_id: Mapped[Optional[int]] = mapped_column(BigInt)
    worker_id: Mapped[Optional[int]] = mapped_column(BigInt)
    chunk_id: Mapped[Optional[int]] = mapped_column(BigInt)
    external_id: Mapped[Optional[int]] = mapped_column(BigInt)
    operation: Mapped[str] = mapped_column(String(64))
    result: Mapped[Optional[str]] = mapped_column(String(32))
    error_code: Mapped[Optional[str]] = mapped_column(String(64))
    message: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index("ix_events_ts", "ts"),
        Index("ix_events_job_ts", "job_id", "ts"),
        Index("ix_events_worker_ts", "worker_id", "ts"),
    )


class AuditLog(Base):
    """Журнал действий пользователей. В details не попадают секреты."""

    __tablename__ = "audit_log"

    id: Mapped[int] = _pk()
    ts: Mapped[dt.datetime] = _now()
    user_id: Mapped[Optional[int]] = mapped_column(BigInt, ForeignKey("users.id", ondelete="SET NULL"))
    username: Mapped[Optional[str]] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[Optional[str]] = mapped_column(String(32))
    target_id: Mapped[Optional[str]] = mapped_column(String(64))
    ip: Mapped[Optional[str]] = mapped_column(String(64))
    details: Mapped[Optional[dict]] = mapped_column(Json)

    __table_args__ = (Index("ix_audit_log_ts", "ts"),)
