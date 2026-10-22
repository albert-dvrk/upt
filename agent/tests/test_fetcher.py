import hashlib
import os

import pytest

from fake_target import content
from videofetch_agent.fetcher import Stop


def test_check_statuses(fetcher, settings):
    assert fetcher.check(404, settings)[0].status == "not_found"
    assert fetcher.check(5, settings)[0].status == "forbidden"
    outcome, meta = fetcher.check(1, settings)
    assert outcome.status == "found" and meta["id"] == 1


def test_server_errors_are_retried_then_succeed(fetcher, settings, target):
    target.fail_next_pages = 2
    assert fetcher.check(1, settings)[0].status == "found"
    assert target.page_hits == 3


def test_persistent_server_errors_become_failed(fetcher, settings, target):
    target.fail_next_pages = 100
    outcome, _ = fetcher.check(1, settings)
    assert (outcome.status, outcome.error_code) == ("failed", "http_5xx")
    assert target.page_hits == settings.max_retries + 1


def test_rate_limit_waits_and_retries(fetcher, settings, target):
    target.rate_limit_pages = 3
    outcome, _ = fetcher.check(1, settings)
    assert outcome.status == "found"
    assert target.page_hits == 4


def test_rate_limit_exhausted(fetcher, settings, target):
    target.rate_limit_pages = 100
    settings.max_rate_limit_waits = 2
    assert fetcher.check(1, settings)[0].status == "rate_limited"


def test_unreachable_target_is_failed_not_exception(fetcher, settings):
    settings.url_template = "http://127.0.0.1:1/{id}"  # порт закрыт
    outcome, _ = fetcher.check(1, settings)
    assert (outcome.status, outcome.error_code) == ("failed", "network")


def test_download_verifies_and_publishes_atomically(fetcher, settings):
    _, meta = fetcher.check(1, settings)
    out = fetcher.download(1, meta, settings)
    assert out.status == "completed"
    path = fetcher.target_path(1, meta, settings)
    assert open(path, "rb").read() == content(1, 300_000)
    assert not os.path.exists(path + ".part")


def test_download_resumes_with_range_after_drop(fetcher, settings, target):
    _, meta = fetcher.check(2, settings)
    target.drop_next_files = 1
    out = fetcher.download(2, meta, settings)
    assert out.status == "completed"
    assert target.ranges[0] is None and target.ranges[1].startswith("bytes=") and target.ranges[1] != "bytes=0-"
    assert open(fetcher.target_path(2, meta, settings), "rb").read() == content(2, 300_000)


def test_download_resumes_existing_part_file(fetcher, settings, target):
    _, meta = fetcher.check(3, settings)
    final = fetcher.target_path(3, meta, settings)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    with open(final + ".part", "wb") as fh:
        fh.write(content(3, 300_000)[:100_000])
    assert fetcher.download(3, meta, settings).status == "completed"
    assert target.ranges == ["bytes=100000-"]


def test_checksum_mismatch_removes_file(fetcher, settings, target):
    target.corrupt = {7}
    _, meta = fetcher.check(7, settings)
    out = fetcher.download(7, meta, settings)
    assert (out.status, out.error_code) == ("failed", "checksum_mismatch")
    final = fetcher.target_path(7, meta, settings)
    assert not os.path.exists(final) and not os.path.exists(final + ".part")


def test_already_downloaded_file_is_not_fetched_again(fetcher, settings, target):
    _, meta = fetcher.check(1, settings)
    fetcher.download(1, meta, settings)
    hits = target.file_hits
    assert fetcher.download(1, meta, settings).status == "completed"
    assert target.file_hits == hits


def test_hostile_title_stays_inside_storage(fetcher, settings, target, tmp_path):
    target.titles[1] = "../../../etc/cron.d/evil"
    _, meta = fetcher.check(1, settings)
    assert fetcher.download(1, meta, settings).status == "completed"
    root = os.path.realpath(settings.storage_path)
    files = os.listdir(root)
    assert len(files) == 1 and os.path.realpath(os.path.join(root, files[0])).startswith(root)
    assert not os.path.exists(tmp_path / "etc")


def test_download_host_must_match_target(fetcher, settings):
    _, meta = fetcher.check(1, settings)
    meta["download_url"] = "http://evil.example/x.mp4"
    out = fetcher.download(1, meta, settings)
    assert out.error_code == "download_host_not_allowed"


def test_disk_space_check(fetcher, settings):
    _, meta = fetcher.check(1, settings)
    settings.min_free_bytes = 10**18
    assert fetcher.download(1, meta, settings).error_code == "disk_full"


def test_stop_interrupts(fetcher, settings):
    fetcher.stop_event.set()
    with pytest.raises(Stop):
        fetcher.check(1, settings)


def test_digest_helper_matches():
    assert hashlib.sha256(content(1, 10)).hexdigest()
