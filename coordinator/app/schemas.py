"""Схемы запросов и ответов HTTP API."""

from __future__ import annotations

import datetime as dt
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.statuses import ItemStatus


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def _check_storage_path(value: str | None) -> str | None:
    if value is None:
        return None
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("storage_path должен быть абсолютным путём без '..'")
    return str(path)


# ------------------------------------------------------------------ серверы


class WorkerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    host: str = Field(min_length=1, max_length=255)
    ssh_port: int = Field(22, ge=1, le=65535)
    ssh_user: str = Field(min_length=1, max_length=64)
    auth_method: str = Field("key", pattern=r"^key$")
    ssh_key_ref: str | None = Field(None, max_length=255)  # ссылка на секрет, не сам ключ
    threads: int = Field(4, ge=1, le=1024)
    storage_path: str = Field(max_length=512)
    min_free_space_gb: int = Field(20, ge=0)

    _check_path = field_validator("storage_path")(_check_storage_path)


class WorkerPatch(BaseModel):
    host: str | None = Field(None, min_length=1, max_length=255)
    ssh_port: int | None = Field(None, ge=1, le=65535)
    ssh_user: str | None = Field(None, min_length=1, max_length=64)
    ssh_key_ref: str | None = Field(None, max_length=255)
    threads: int | None = Field(None, ge=1, le=1024)
    storage_path: str | None = Field(None, max_length=512)
    min_free_space_gb: int | None = Field(None, ge=0)
    enabled: bool | None = None

    _check_path = field_validator("storage_path")(_check_storage_path)


class WorkerOut(ORMModel):
    id: int
    name: str
    host: str
    ssh_port: int
    ssh_user: str
    auth_method: str
    threads: int
    storage_path: str
    min_free_space_gb: int
    status: str
    enabled: bool
    agent_version: str | None
    last_heartbeat_at: dt.datetime | None
    resources: dict[str, Any] | None
    created_at: dt.datetime
    updated_at: dt.datetime


class RegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    agent_version: str = Field(min_length=1, max_length=32)
    resources: dict[str, Any] | None = None


class RegisterResponse(BaseModel):
    worker_id: int
    name: str
    threads: int
    storage_path: str
    min_free_space_gb: int
    lease_seconds: int
    heartbeat_interval_seconds: int


class MetricsIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    cpu_percent: float | None = None
    load1: float | None = None
    ram_total: int | None = None
    ram_used: int | None = None
    swap_used: int | None = None
    disk_total: int | None = None
    disk_free: int | None = None
    net_rx_bps: float | None = None
    net_tx_bps: float | None = None
    disk_read_bps: float | None = None
    disk_write_bps: float | None = None
    open_files: int | None = None
    active_connections: int | None = None
    active_checks: int | None = None
    active_downloads: int | None = None
    ids_per_sec: float | None = None
    download_bps: float | None = None
    http_status_counts: dict[str, int] | None = None


class MetricOut(ORMModel):
    id: int
    worker_id: int
    collected_at: dt.datetime
    cpu_percent: float | None
    load1: float | None
    ram_total: int | None
    ram_used: int | None
    swap_used: int | None
    disk_total: int | None
    disk_free: int | None
    net_rx_bps: float | None
    net_tx_bps: float | None
    disk_read_bps: float | None
    disk_write_bps: float | None
    open_files: int | None
    active_connections: int | None
    active_checks: int | None
    active_downloads: int | None
    ids_per_sec: float | None
    download_bps: float | None
    http_status_counts: dict[str, Any] | None


class HeartbeatRequest(BaseModel):
    agent_version: str | None = Field(None, max_length=32)
    resources: dict[str, Any] | None = None
    metrics: MetricsIn | None = None


class HeartbeatJobInfo(BaseModel):
    job_id: int
    status: str
    threads: int
    rate_limit_rps: float | None


class HeartbeatResponse(BaseModel):
    server_time: dt.datetime
    lease_seconds: int
    jobs: list[HeartbeatJobInfo]


# ------------------------------------------------------------------ блоки и результаты


_SHA256 = r"^[0-9a-f]{64}$"


class MediaIn(BaseModel):
    """Метаданные найденного материала (раздел 8 задания)."""

    title: str | None = Field(None, max_length=1000)  # исходное название; в путях не используется
    page_url: str = Field(max_length=1024)
    download_url: str | None = Field(None, max_length=2048)
    mime_type: str | None = Field(None, max_length=128)
    size_bytes: int | None = Field(None, ge=0)
    checksum_sha256: str | None = Field(None, pattern=_SHA256)


class FileIn(BaseModel):
    """Скачанный файл. Путь — относительный (от каталога хранения сервера), без «..» и абсолютных частей."""

    storage_path: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(ge=0)
    mime_type: str | None = Field(None, max_length=128)
    checksum_sha256: str = Field(pattern=_SHA256)

    @field_validator("storage_path")
    @classmethod
    def _safe_path(cls, v: str) -> str:
        parts = v.split("/")
        if v.startswith("/") or "\\" in v or "\x00" in v or any(p in ("", ".", "..") for p in parts):
            raise ValueError("Недопустимый путь файла: нужен относительный путь без «..»")
        return v


class ItemIn(BaseModel):
    external_id: int = Field(ge=0)
    status: ItemStatus
    http_status: int | None = Field(None, ge=100, le=599)
    error_code: str | None = Field(None, max_length=64)
    media: MediaIn | None = None
    file: FileIn | None = None


class ChunkSettings(BaseModel):
    url_template: str | None
    threads: int
    rate_limit_rps: float | None
    request_timeout_seconds: int
    max_retries: int
    storage_path: str


class ChunkLease(BaseModel):
    chunk_id: int
    job_id: int
    range_start: int
    range_end: int
    next_id: int
    attempt: int
    lease_expires_at: dt.datetime
    settings: ChunkSettings


class LeaseResponse(BaseModel):
    chunk: ChunkLease | None
    reason: str | None


class ProgressRequest(BaseModel):
    chunk_id: int
    next_id: int = Field(ge=0)
    items: list[ItemIn] = Field(default_factory=list, max_length=1000)


class ProgressResponse(BaseModel):
    lease_expires_at: dt.datetime
    job_status: str
    settings: ChunkSettings


class CompleteRequest(BaseModel):
    chunk_id: int
    items: list[ItemIn] = Field(default_factory=list, max_length=1000)


class CompleteResponse(BaseModel):
    job_status: str


class FailRequest(BaseModel):
    chunk_id: int
    error_code: str = Field(min_length=1, max_length=64)
    message: str | None = Field(None, max_length=2000)
    retryable: bool = True


class FailResponse(BaseModel):
    chunk_status: str
    job_status: str


class ChunkOut(ORMModel):
    id: int
    job_id: int
    range_start: int
    range_end: int
    next_id: int
    status: str
    leased_by: int | None
    lease_expires_at: dt.datetime | None
    attempt: int


class ItemOut(ORMModel):
    external_id: int
    status: str
    http_status: int | None
    error_code: str | None
    worker_id: int | None
    attempts: int
    checked_at: dt.datetime | None


class ItemPage(BaseModel):
    total: int
    items: list[ItemOut]


class EventOut(ORMModel):
    id: int
    ts: dt.datetime
    level: str
    component: str
    job_id: int | None
    worker_id: int | None
    chunk_id: int | None
    external_id: int | None
    operation: str
    result: str | None
    error_code: str | None
    message: str | None


# ------------------------------------------------------------------ задания


class JobWorkerIn(BaseModel):
    worker_id: int
    threads: int | None = Field(None, ge=1, le=1024)
    rate_limit_rps: float | None = Field(None, gt=0)


class JobWorkerPatch(BaseModel):
    threads: int | None = Field(None, ge=1, le=1024)
    rate_limit_rps: float | None = Field(None, gt=0)
    enabled: bool | None = None


class JobWorkerOut(BaseModel):
    worker_id: int
    name: str
    enabled: bool
    worker_status: str
    threads: int
    rate_limit_rps: float | None


class JobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    # Диапазон: строка ("[A,B]", "A", "[A,]", "") либо пара range_from / range_to.
    range: str | None = None
    range_from: int | None = Field(None, ge=0)
    range_to: int | None = Field(None, ge=0)
    chunk_size: int | None = Field(None, ge=1, le=1_000_000)
    request_timeout_seconds: int = Field(20, ge=1, le=600)
    max_retries: int = Field(4, ge=0, le=20)
    global_rate_limit_rps: float | None = Field(None, gt=0)
    threads: int | None = Field(None, ge=1, le=1024)  # глобальное значение; у сервера может быть своё
    url_template: str | None = Field(None, max_length=512)
    max_ids: int | None = Field(None, ge=1)
    max_runtime_seconds: int | None = Field(None, ge=1)
    workers: list[JobWorkerIn] = Field(default_factory=list, max_length=1000)


class JobPatch(BaseModel):
    threads: int | None = Field(None, ge=1, le=1024)
    global_rate_limit_rps: float | None = Field(None, gt=0)
    request_timeout_seconds: int | None = Field(None, ge=1, le=600)
    max_retries: int | None = Field(None, ge=0, le=20)
    chunk_size: int | None = Field(None, ge=1, le=1_000_000)


class JobOut(ORMModel):
    id: int
    name: str
    status: str
    attempt: int
    range_from: int
    range_to: int | None
    chunk_size: int
    request_timeout_seconds: int
    max_retries: int
    global_rate_limit_rps: float | None
    max_ids: int | None
    max_runtime_seconds: int | None
    config: dict[str, Any] | None
    created_at: dt.datetime
    started_at: dt.datetime | None
    finished_at: dt.datetime | None


class JobDetail(JobOut):
    stats: dict[str, Any]
    workers: list[JobWorkerOut]


class FileOut(BaseModel):
    external_id: int
    title: str | None
    mime_type: str | None
    size_bytes: int
    checksum_sha256: str
    storage_path: str
    worker_id: int | None
    completed_at: dt.datetime


class FilePage(BaseModel):
    total: int
    files: list[FileOut]
