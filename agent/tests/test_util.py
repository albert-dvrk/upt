import datetime as dt
import os
import random

import pytest

from videofetch_agent.util import RateGate, backoff_delay, parse_retry_after, safe_join, sanitize_title, video_filename

NASTY = [
    "../../etc/passwd", "a/b\\c", "CON", "nul.txt", "tab\tand\nnewline", "Видео ‮ с управляющими символами",
    "x" * 500, "   ", "...", "Отчёт: итоги / планы?.mp4", "", None, "\x00evil",
]


@pytest.mark.parametrize("title", NASTY)
def test_sanitized_title_is_always_a_plain_safe_name(title):
    name = sanitize_title(title)
    assert name and "/" not in name and "\\" not in name and ".." not in name
    assert not name.startswith(".") and name == name.strip()
    assert all(ord(c) >= 32 and ord(c) != 127 for c in name)
    assert len(name.encode()) <= 120
    assert name.upper().split(".")[0] not in {"CON", "NUL", "PRN", "AUX"}


def test_unicode_is_kept(tmp_path):
    assert sanitize_title("Вводная лекция") == "Вводная лекция"
    assert video_filename(7, "Lecture 01: Intro") == "7_Lecture 01_ Intro.mp4"


def test_safe_join_blocks_escape(tmp_path):
    assert safe_join(str(tmp_path), "a.mp4").startswith(str(tmp_path.resolve()))
    with pytest.raises(ValueError):
        safe_join(str(tmp_path), "..", "evil")
    with pytest.raises(ValueError):
        safe_join(str(tmp_path), "/etc/passwd")
    link = tmp_path / "link"
    os.symlink("/", link)
    with pytest.raises(ValueError):
        safe_join(str(tmp_path), "link", "etc")


def test_backoff_is_bounded_and_grows():
    rnd = random.Random(1)
    assert all(0 <= backoff_delay(1, 0.5, 30, rnd) <= 0.5 for _ in range(50))
    assert max(backoff_delay(4, 0.5, 30, rnd) for _ in range(200)) > 2
    assert all(backoff_delay(30, 0.5, 30, rnd) <= 30 for _ in range(50))


def test_retry_after_formats():
    assert parse_retry_after("3") == 3
    assert parse_retry_after(None, default=7) == 7
    assert parse_retry_after("мусор", default=2) == 2
    assert parse_retry_after("9999") == 120  # потолок
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 20 <= parse_retry_after(future) <= 31


def test_rate_gate_spaces_requests_and_blocks():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    gate = RateGate(rate=10, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        gate.wait()
    assert slept == pytest.approx([0.1, 0.1])  # 10 запросов/с
    gate.block_for(5)
    slept.clear()
    gate.wait()
    assert slept[0] >= 4.9
