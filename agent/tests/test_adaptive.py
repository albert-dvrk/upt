"""Адаптивное снижение нагрузки и прогресс активных загрузок."""

from fake_target import content
from videofetch_agent.fetcher import Fetcher
from videofetch_agent.processor import Counters
from videofetch_agent.util import RateGate
import threading


def make_gate():
    now = [0.0]
    return RateGate(clock=lambda: now[0], sleep=lambda s: None), now


def test_429_halves_the_load_immediately_with_cooldown():
    gate, now = make_gate()
    gate.report(429)
    assert gate.scale == 0.5
    gate.report(429)  # в пределах 2 с повторно не режем: один сигнал от всех потоков
    assert gate.scale == 0.5
    now[0] = 3.0
    gate.report(429)
    assert gate.scale == 0.25


def test_server_errors_reduce_by_share_in_window():
    gate, _ = make_gate()
    for i in range(20):
        gate.report(500 if i % 4 == 0 else 200)  # 25% плохих ≥ 20%
    assert gate.scale == 0.5


def test_few_errors_do_not_reduce():
    gate, _ = make_gate()
    for i in range(40):
        gate.report(503 if i == 3 else 200)  # 1 из 20 — ниже порога
    assert gate.scale == 1.0


def test_scale_recovers_gradually_and_has_a_floor():
    gate, now = make_gate()
    for i in range(10):
        now[0] += 3
        gate.report(429)
    assert gate.scale == RateGate.MIN_SCALE
    for _ in range(20):
        gate.report(200)
    assert abs(gate.scale - 0.2) < 1e-9  # +0.1 за спокойное окно
    for _ in range(20 * 20):
        gate.report(200)
    assert gate.scale == 1.0


def test_rate_limit_interval_follows_scale():
    gate, now = make_gate()
    gate.set_rate(10)  # 0,1 с между запросами
    gate.report(429)   # масштаб 0,5 → 0,2 с
    gate.wait()
    assert abs(gate.wait() - 0.2) < 1e-9


def test_network_error_counts_as_bad():
    gate, _ = make_gate()
    for _ in range(20):
        gate.report(None)
    assert gate.scale == 0.5


def test_fetcher_reports_429_to_gate(target, settings):
    gate = RateGate()
    f = Fetcher(gate, threading.Event(), sleep=lambda s: None)
    target.rate_limit_pages = 1
    f.check(1, settings)
    assert gate.scale < 1.0


def test_download_progress_is_tracked_and_cleared(target, settings):
    counters = Counters()
    seen = []

    class Spy(Counters):
        def dl_add(self, vid, n):
            super().dl_add(vid, n)
            seen.append(self.downloads_snapshot())

    counters = Spy()
    f = Fetcher(RateGate(), threading.Event(), sleep=lambda s: None, counters=counters, job_id=7)
    outcome, meta = f.check(1, settings)
    assert f.download(1, meta, settings).status == "completed"
    assert seen, "прогресс не сообщался"
    last = seen[-1][0]
    assert last["external_id"] == 1 and last["job_id"] == 7 and last["bytes"] > 0
    assert last["total"] == meta["size_bytes"] and last["bytes"] <= last["total"]
    assert counters.downloads_snapshot() == []  # после завершения запись убрана
