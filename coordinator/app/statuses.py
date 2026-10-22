"""Статусы сущностей и формальные переходы состояний задания (разделы 7 и 19)."""

from __future__ import annotations

from enum import StrEnum


class ItemStatus(StrEnum):
    PENDING = "pending"
    CHECKING = "checking"
    NOT_FOUND = "not_found"
    FOUND = "found"
    DOWNLOAD_QUEUED = "download_queued"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    RETRY_WAIT = "retry_wait"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChunkStatus(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"


class JobStatus(StrEnum):
    CREATED = "created"
    VALIDATING = "validating"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkerStatus(StrEnum):
    OFFLINE = "offline"
    ONLINE = "online"
    DEGRADED = "degraded"
    DISABLED = "disabled"


# Завершённые состояния — конечные. Повторный запуск возможен только через
# явную операцию «перезапуск», которая создаёт новую попытку (новое задание).
JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.CREATED: frozenset({JobStatus.VALIDATING, JobStatus.STOPPED}),
    JobStatus.VALIDATING: frozenset({JobStatus.RUNNING, JobStatus.FAILED}),
    JobStatus.RUNNING: frozenset(
        {JobStatus.PAUSING, JobStatus.STOPPING, JobStatus.COMPLETED, JobStatus.FAILED}
    ),
    JobStatus.PAUSING: frozenset({JobStatus.PAUSED, JobStatus.STOPPING, JobStatus.FAILED}),
    JobStatus.PAUSED: frozenset({JobStatus.RUNNING, JobStatus.STOPPING, JobStatus.FAILED}),
    JobStatus.STOPPING: frozenset({JobStatus.STOPPED, JobStatus.FAILED}),
    JobStatus.STOPPED: frozenset(),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
}

TERMINAL_JOB_STATUSES = frozenset(s for s, nxt in JOB_TRANSITIONS.items() if not nxt)


class InvalidTransition(ValueError):
    pass


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    return target in JOB_TRANSITIONS[current]


def ensure_transition(current: JobStatus, target: JobStatus) -> JobStatus:
    if not can_transition(current, target):
        raise InvalidTransition(f"Переход задания {current} -> {target} запрещён")
    return target
