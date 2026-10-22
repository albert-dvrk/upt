import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from mock_service.app import Behavior, content_bytes, create_app, file_size, page_kind


@pytest.fixture
def client():
    with TestClient(create_app(Behavior(seed=7))) as c:
        yield c


def find_id(kind: str, seed: int = 7, limit: int = 5000) -> int:
    b = Behavior(seed=seed)
    for vid in range(limit):
        if page_kind(b, vid) == kind:
            return vid
    raise AssertionError(f"не нашли id вида {kind}")


def test_page_kind_is_deterministic_and_matches_http(client):
    for kind, code in (("found", 200), ("forbidden", 403), ("missing", 404)):
        vid = find_id(kind)
        assert client.get(f"/{vid}").status_code == code
        assert client.get(f"/{vid}").status_code == code  # повтор даёт тот же результат


def test_distribution_is_close_to_config():
    b = Behavior(seed=3)
    kinds = [page_kind(b, i) for i in range(5000)]
    assert 0.17 < kinds.count("found") / 5000 < 0.23
    assert 0.01 < kinds.count("forbidden") / 5000 < 0.05


def test_found_page_metadata_and_full_download(client):
    vid = find_id("found")
    meta = client.get(f"/{vid}").json()
    assert meta["id"] == vid and meta["mime_type"] == "video/mp4"
    assert meta["size_bytes"] == file_size(Behavior(seed=7), vid)

    r = client.get(meta["download_url"].replace("http://testserver", ""))
    assert r.status_code == 200
    assert len(r.content) == meta["size_bytes"]
    assert hashlib.sha256(r.content).hexdigest() == meta["sha256"]


def test_range_requests(client):
    vid = find_id("found")
    url = f"/files/{vid}/video.mp4"
    full = client.get(url).content
    size = len(full)

    r = client.get(url, headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["Content-Range"] == f"bytes 100-199/{size}"
    assert r.content == full[100:200]

    r = client.get(url, headers={"Range": f"bytes={size - 10}-"})
    assert r.status_code == 206 and r.content == full[-10:]

    r = client.get(url, headers={"Range": "bytes=-5"})
    assert r.status_code == 206 and r.content == full[-5:]

    r = client.get(url, headers={"Range": f"bytes={size + 5}-"})
    assert r.status_code == 416


def test_content_is_range_consistent():
    # граница блока не должна ломать склейку
    a = content_bytes(1, 5, 0, 70000)
    b = content_bytes(1, 5, 65000, 70000)
    assert a[65000:] == b


def test_head_returns_size_without_body(client):
    vid = find_id("found")
    r = client.head(f"/files/{vid}/video.mp4")
    assert r.status_code == 200 and r.content == b""
    assert int(r.headers["Content-Length"]) == file_size(Behavior(seed=7), vid)


def test_rate_limit_then_recovery(client):
    vid = find_id("found")
    client.post("/_control/rate_limit", json={"seconds": 30, "retry_after_seconds": 4})
    r = client.get(f"/{vid}")
    assert r.status_code == 429 and r.headers["Retry-After"] == "4"
    assert client.get(f"/files/{vid}/video.mp4").status_code == 429

    client.post("/_control/rate_limit", json={"seconds": 0})
    assert client.get(f"/{vid}").status_code == 200


def test_rate_limit_expires_by_itself(client):
    vid = find_id("found")
    client.post("/_control/rate_limit", json={"seconds": 0.2})
    assert client.get(f"/{vid}").status_code == 429
    time.sleep(0.3)
    assert client.get(f"/{vid}").status_code == 200


def test_random_server_errors(client):
    client.post("/_control/config", json={"server_error_permille": 1000})
    assert client.get(f"/{find_id('found')}").status_code == 500


def test_delay(client):
    client.post("/_control/config", json={"delay_ms": 150})
    t0 = time.monotonic()
    client.get(f"/{find_id('missing')}")
    assert time.monotonic() - t0 >= 0.14


def test_dropped_download_is_truncated():
    app = create_app(Behavior(seed=7, drop_permille=1000, file_min_kb=512, file_max_kb=512))
    vid = find_id("found")
    with TestClient(app, raise_server_exceptions=False) as c:
        try:
            r = c.get(f"/files/{vid}/video.mp4")
        except Exception:
            return  # обрыв дошёл до клиента исключением — это тоже «обрыв»
        assert len(r.content) < 512 * 1024


def test_stats_and_reset(client):
    client.get(f"/{find_id('found')}")
    client.get(f"/{find_id('missing')}")
    counts = client.get("/_control/stats").json()["status_counts"]
    assert counts["200"] == 1 and counts["404"] == 1

    client.post("/_control/config", json={"delay_ms": 10})
    client.post("/_control/reset")
    assert client.get("/_control/state").json()["delay_ms"] == 0
    assert client.get("/_control/stats").json()["status_counts"] == {}


def test_control_validation(client):
    assert client.post("/_control/config", json={"found_permille": 5000}).status_code == 422


def test_non_numeric_path_is_not_a_video(client):
    assert client.get("/abc").status_code == 404
