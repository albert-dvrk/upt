import threading

import pytest

from fake_target import Target
from videofetch_agent.fetcher import FetchSettings, Fetcher
from videofetch_agent.util import RateGate


@pytest.fixture
def target():
    t = Target().start()
    yield t
    t.stop()


@pytest.fixture
def settings(target, tmp_path):
    return FetchSettings(url_template=target.template, storage_path=str(tmp_path / "videos"), request_timeout=5, max_retries=3)


@pytest.fixture
def fetcher():
    stop = threading.Event()
    f = Fetcher(RateGate(), stop, sleep=lambda s: None)  # без реальных пауз
    f.stop_event = stop
    return f
