from __future__ import annotations

import os
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
import time
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest

from app.schemas import Incident, RunRecord
from app.store import PostgresStore
from app.migrations import LATEST_SCHEMA_VERSION
from app.retrieval import create_retriever
import app.store as store_module


POSTGRES_URL = os.getenv("HARBOR_TEST_POSTGRES_URL")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set HARBOR_TEST_POSTGRES_URL to a dedicated PostgreSQL test database",
)
def test_pgvector_initialization_serializes_parallel_process_startup() -> None:
    assert POSTGRES_URL is not None
    with psycopg.connect(POSTGRES_URL) as connection, connection.cursor() as cursor:
        cursor.execute("DROP EXTENSION IF EXISTS vector CASCADE")

    barrier = Barrier(3)
    failures: list[BaseException] = []
    backends: list[str] = []
    guard = Lock()

    def initialize() -> None:
        barrier.wait()
        try:
            retriever = create_retriever(
                PROJECT_ROOT / "data",
                "pgvector",
                POSTGRES_URL,
            )
            with guard:
                backends.append(retriever.backend_name)
        except BaseException as exc:  # preserve the original cross-thread failure
            with guard:
                failures.append(exc)

    threads = [Thread(target=initialize) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert len(backends) == 2
    assert all("postgres-pgvector" in backend for backend in backends)


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set HARBOR_TEST_POSTGRES_URL to a dedicated PostgreSQL test database",
)
def test_postgres_schema_migrations_and_worker_registry() -> None:
    assert POSTGRES_URL is not None
    store = PostgresStore(POSTGRES_URL)
    store.initialize()
    suffix = uuid4().hex[:12]
    worker_id = f"pg-registry-{suffix}"
    try:
        assert store.schema_status() == {
            "status": "current",
            "current_version": LATEST_SCHEMA_VERSION,
            "latest_version": LATEST_SCHEMA_VERSION,
        }
        store.heartbeat_worker(worker_id, {"replica": suffix})
        workers = store.list_live_workers(30)
        selected = [worker for worker in workers if worker["worker_id"] == worker_id]
        assert len(selected) == 1
        assert selected[0]["metadata"] == {"replica": suffix}
    finally:
        store.remove_worker(worker_id)


def _postgres_incident() -> Incident:
    return Incident(
        title="PostgreSQL 租户查询回归测试",
        summary="验证租户过滤和 limit 在数据库层执行。",
        severity="P2",
        service="postgres-test",
        environment="lab",
        symptoms=["租户过滤"],
    )


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set HARBOR_TEST_POSTGRES_URL to a dedicated PostgreSQL test database",
)
def test_postgres_lists_runs_with_tenant_filter_and_database_limit() -> None:
    assert POSTGRES_URL is not None
    store = PostgresStore(POSTGRES_URL)
    store.initialize()
    suffix = uuid4().hex[:10]
    tenant_a = f"tenant-a-{suffix}"
    tenant_b = f"tenant-b-{suffix}"
    runs = [
        RunRecord(id=f"RUN-PG-LIST-A1-{suffix}", tenant_id=tenant_a, incident=_postgres_incident()),
        RunRecord(id=f"RUN-PG-LIST-B1-{suffix}", tenant_id=tenant_b, incident=_postgres_incident()),
        RunRecord(id=f"RUN-PG-LIST-A2-{suffix}", tenant_id=tenant_a, incident=_postgres_incident()),
    ]
    try:
        for run in runs:
            store.save_run(run)
        selected = store.list_runs(tenant_a, limit=1)
        assert len(selected) == 1
        assert selected[0].tenant_id == tenant_a
        assert store.list_runs(f"missing-{suffix}", limit=10) == []
    finally:
        with psycopg.connect(POSTGRES_URL) as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM agent_runs WHERE id = ANY(%s)",
                ([run.id for run in runs],),
            )


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


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set HARBOR_TEST_POSTGRES_URL to a dedicated PostgreSQL test database",
)
def test_postgres_atomic_run_job_rollback_and_active_job_dedupe(monkeypatch) -> None:
    assert POSTGRES_URL is not None
    store = PostgresStore(POSTGRES_URL)
    store.initialize()
    suffix = uuid4().hex[:12].upper()
    blocker = RunRecord(
        id=f"RUN-PG-BLOCKER-{suffix}",
        incident=Incident(
            title="PostgreSQL 原子提交阻断记录",
            summary="使用确定性任务主键冲突验证 Run 与 Job 在同一事务回滚。",
            severity="P2",
            service="postgres-atomic",
            environment="lab",
            symptoms=["模拟任务插入失败"],
        ),
    )
    target = blocker.model_copy(
        update={"id": f"RUN-PG-TARGET-{suffix}", "version": 0}
    )
    dedupe = blocker.model_copy(
        update={"id": f"RUN-PG-DEDUPE-{suffix}", "version": 0}
    )
    collision_hex = "d" * 32
    ids = [blocker.id, target.id, dedupe.id]
    try:
        store.save_run(blocker)
        monkeypatch.setattr(
            store_module, "uuid4", lambda: SimpleNamespace(hex=collision_hex)
        )
        store.enqueue_job(blocker.id, "start")

        with pytest.raises(psycopg.errors.UniqueViolation):
            store.save_run_and_enqueue(target, "start")
        assert target.version == 0
        assert store.get_run(target.id) is None

        monkeypatch.undo()
        store.save_run(dedupe)
        first = store.enqueue_job(dedupe.id, "start")
        second = store.enqueue_job(dedupe.id, "start")
        assert second.id == first.id
        assert len(store.list_jobs(dedupe.id)) == 1
    finally:
        with psycopg.connect(POSTGRES_URL) as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM agent_jobs WHERE run_id = ANY(%s)", (ids,))
            cursor.execute("DELETE FROM agent_runs WHERE id = ANY(%s)", (ids,))
