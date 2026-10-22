import pytest

from app.statuses import (
    JOB_TRANSITIONS,
    TERMINAL_JOB_STATUSES,
    InvalidTransition,
    ItemStatus,
    JobStatus,
    can_transition,
    ensure_transition,
)


def test_every_job_status_has_transition_entry():
    assert set(JOB_TRANSITIONS) == set(JobStatus)


def test_happy_path():
    path = [
        JobStatus.CREATED,
        JobStatus.VALIDATING,
        JobStatus.RUNNING,
        JobStatus.PAUSING,
        JobStatus.PAUSED,
        JobStatus.RUNNING,
        JobStatus.STOPPING,
        JobStatus.STOPPED,
    ]
    for cur, nxt in zip(path, path[1:]):
        assert ensure_transition(cur, nxt) == nxt


def test_completed_job_cannot_return_to_running():
    assert JobStatus.COMPLETED in TERMINAL_JOB_STATUSES
    assert not can_transition(JobStatus.COMPLETED, JobStatus.RUNNING)
    with pytest.raises(InvalidTransition):
        ensure_transition(JobStatus.COMPLETED, JobStatus.RUNNING)


@pytest.mark.parametrize("terminal", [JobStatus.STOPPED, JobStatus.COMPLETED, JobStatus.FAILED])
def test_terminal_states_have_no_exits(terminal):
    assert all(not can_transition(terminal, s) for s in JobStatus)


@pytest.mark.parametrize(
    "cur, nxt",
    [
        (JobStatus.CREATED, JobStatus.RUNNING),  # мимо validating
        (JobStatus.PAUSED, JobStatus.COMPLETED),
        (JobStatus.RUNNING, JobStatus.PAUSED),  # нужно пройти через pausing
    ],
)
def test_forbidden_shortcuts(cur, nxt):
    assert not can_transition(cur, nxt)


def test_item_statuses_match_spec():
    assert {s.value for s in ItemStatus} == {
        "pending", "checking", "not_found", "found", "download_queued", "downloading",
        "completed", "skipped", "forbidden", "rate_limited", "retry_wait", "failed", "cancelled",
    }
