from __future__ import annotations

import os
from threading import Barrier, Event, Lock, Thread
import time
from uuid import uuid4

import psycopg
import pytest

from app.schemas import Incident, RunRecord
from app.store import PostgresStore


POSTGRES_URL = os.getenv("HARBOR_TEST_POSTGRES_URL")


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set HARBOR_TEST_POSTGRES_URL to a dedicated PostgreSQL test database",
)
def test_concurrent_postgres_workers_claim_exactly_once() -> None:
    """Exercise the PostgreSQL-only UPDATE ... FROM / SKIP LOCKED path."""
    assert POSTGRES_URL is not None
    store = PostgresStore(POSTGRES_URL)
    store.initialize()
    suffix = uuid4().hex[:12].upper()
    run = RunRecord(
        id=f"RUN-PG-{suffix}",
        incident=Incident(
            title="PostgreSQL 并发领取回归测试",
            summary="验证真实 PostgreSQL 的 SKIP LOCKED、RETURNING 与 fencing token。",
            severity="P2",
            service="postgres-test",
            environment="lab",
            symptoms=["并发领取"],
        ),
    )
    store.save_run(run)
    job = store.enqueue_job(run.id, "start")
    worker_count = 16
    barrier = Barrier(worker_count)
    lock = Lock()
    claims = []
    errors: list[BaseException] = []

    def claim(index: int) -> None:
        barrier.wait()
        try:
            result = store.claim_job(f"pg-worker-{suffix}-{index}", 30)
        except BaseException as error:
            with lock:
                errors.append(error)
            return
        if result is not None:
            with lock:
                claims.append(result)

    try:
        threads = [Thread(target=claim, args=(index,)) for index in range(worker_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert len(claims) == 1
        assert claims[0].id == job.id
        assert claims[0].fencing_token == 1
    finally:
        with psycopg.connect(POSTGRES_URL) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM agent_jobs WHERE run_id=%s", (run.id,))
            cursor.execute("DELETE FROM agent_runs WHERE id=%s", (run.id,))


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set HARBOR_TEST_POSTGRES_URL to a dedicated PostgreSQL test database",
)
def test_postgres_model_slot_serializes_worker_replicas() -> None:
    assert POSTGRES_URL is not None
    first_store = PostgresStore(POSTGRES_URL)
    second_store = PostgresStore(POSTGRES_URL)
    first_acquired = Event()
    release_first = Event()
    second_acquired = Event()
    wait_seconds: list[float] = []

    def hold_first_slot() -> None:
        with first_store.model_inference_slot():
            first_acquired.set()
            assert release_first.wait(timeout=3)

    def wait_for_second_slot() -> None:
        assert first_acquired.wait(timeout=3)
        started = time.perf_counter()
        with second_store.model_inference_slot():
            wait_seconds.append(time.perf_counter() - started)
            second_acquired.set()

    first = Thread(target=hold_first_slot)
    second = Thread(target=wait_for_second_slot)
    first.start()
    second.start()
    assert first_acquired.wait(timeout=3)
    assert second_acquired.wait(timeout=0.15) is False
    release_first.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert second_acquired.is_set()
    assert wait_seconds[0] >= 0.1
