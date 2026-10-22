"""Проверки на настоящем PostgreSQL: параллельная выдача блоков и применимость схемы.

Запускаются только при заданном VF_TEST_DATABASE_URL. База должна быть отдельной и
пустой: тест удаляет и пересоздаёт все таблицы. Пример (стенд из deploy/):

    docker compose exec postgres createdb -U videofetch videofetch_test
    export VF_TEST_DATABASE_URL=postgresql+psycopg://videofetch:videofetch@localhost:5433/videofetch_test
    pytest coordinator/tests/test_pg_concurrency.py -v
"""

import os
import threading

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.models import Base, Item, RangeChunk
from app.services import leases
from helpers import finish, lease, make_job, make_worker

URL = os.environ.get("VF_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="нужен VF_TEST_DATABASE_URL (PostgreSQL)")


@pytest.fixture
def pg_factory():
    assert "test" in URL, "Тест пересоздаёт таблицы: в имени базы должно быть 'test'"
    engine = create_engine(URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def test_parallel_workers_never_get_the_same_block(pg_factory):
    with pg_factory() as s:
        ws = [make_worker(s, f"w{i}") for i in range(8)]
        make_job(s, [w.id for w in ws], range="[0,]", chunk_size=10)
        ids = [w.id for w in ws]

    got, errors, lock = [], [], threading.Lock()

    def run(worker_id):
        try:
            for _ in range(25):
                with pg_factory() as s:
                    chunk = leases.lease_chunk(s, worker_id)["chunk"]
                    s.commit()
                    assert chunk is not None
                    from helpers import results_for

                    leases.complete_chunk(
                        s, worker_id, chunk["chunk_id"], results_for(chunk["range_start"], chunk["range_end"])
                    )
                    s.commit()
                with lock:
                    got.append((chunk["range_start"], chunk["range_end"]))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=run, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(got) == 200 and len(set(got)) == 200  # ни один блок не выдан дважды
    assert sorted(start for start, _ in got) == list(range(0, 2000, 10))  # без пропусков

    with pg_factory() as s:
        assert s.scalar(select(func.count()).select_from(RangeChunk)) == 200
        assert s.scalar(select(func.count()).select_from(Item)) == 2000


def test_upsert_is_idempotent_on_postgres(pg_factory):
    from app.schemas import ItemIn
    from app.services.items import upsert_items
    from app.statuses import ItemStatus
    from helpers import T0

    with pg_factory() as s:
        w = make_worker(s)
        make_job(s, [w.id])
        chunk = s.get(RangeChunk, lease(s, w)["chunk"]["chunk_id"])
        batch = [ItemIn(external_id=i, status=ItemStatus.NOT_FOUND, http_status=404) for i in range(10)]
        for _ in range(3):
            upsert_items(s, chunk, w.id, batch, T0)
            s.commit()
        assert s.scalar(select(func.count()).select_from(Item)) == 10

        done = [ItemIn(external_id=3, status=ItemStatus.COMPLETED)]
        late = [ItemIn(external_id=3, status=ItemStatus.FOUND)]
        upsert_items(s, chunk, w.id, done, T0)
        upsert_items(s, chunk, w.id, late, T0)
        s.commit()
        assert s.scalar(select(Item.status).where(Item.external_id == 3)) == "completed"


def test_chunk_completion_flow_on_postgres(pg_factory):
    with pg_factory() as s:
        w = make_worker(s)
        job = make_job(s, [w.id], range="[0,24]", chunk_size=10)
        while (chunk := lease(s, w)["chunk"]) is not None:
            finish(s, w, chunk)
        s.refresh(job)
        assert job.status == "completed"
