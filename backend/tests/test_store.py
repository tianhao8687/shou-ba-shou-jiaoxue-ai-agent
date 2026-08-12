from contextlib import closing
from pathlib import Path
import sqlite3
from threading import Barrier, Lock, Thread

import pytest

from app.schemas import Approval, Incident, JobStatus, RunRecord
from app.store import ConcurrencyError, LeaseLostError, SQLiteStore


def incident() -> Incident:
    return Incident(
        title="api 出现测试异常",
        summary="用于验证持久状态和并发控制的最小自由事件输入。",
        severity="P2",
        service="test-api",
        environment="lab",
        symptoms=["错误率升高"],
    )


def test_sqlite_compare_and_swap_rejects_stale_writer(tmp_path: Path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'cas.db'}")
    store.initialize()
    run = RunRecord(id="RUN-CAS", incident=incident())
    store.save_run(run)
    first = store.get_run(run.id)
    stale = store.get_run(run.id)
    assert first and stale and first.version == stale.version
    first.diagnosis = "first writer"
    store.save_run(first)
    stale.diagnosis = "stale writer"
    with pytest.raises(ConcurrencyError):
        store.save_run(stale)
    current = store.get_run(run.id)
    assert current and current.diagnosis == "first writer"


def test_sqlite_lists_runs_with_tenant_filter_and_database_limit(tmp_path: Path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'tenant-list.db'}")
    store.initialize()
    for run_id, tenant_id in (
        ("RUN-A-1", "tenant-a"),
        ("RUN-B-1", "tenant-b"),
        ("RUN-A-2", "tenant-a"),
    ):
        store.save_run(RunRecord(id=run_id, tenant_id=tenant_id, incident=incident()))

    tenant_runs = store.list_runs("tenant-a", limit=1)
    assert len(tenant_runs) == 1
    assert tenant_runs[0].tenant_id == "tenant-a"
    assert store.list_runs("missing-tenant", limit=10) == []


def test_concurrent_workers_claim_a_job_only_once(tmp_path: Path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'claim.db'}")
    store.initialize()
    run = RunRecord(id="RUN-CLAIM", incident=incident())
    store.save_run(run)
    job = store.enqueue_job(run.id, "start")
    worker_count = 32
    barrier = Barrier(worker_count)
    lock = Lock()
    claims = []

    def claim(index: int) -> None:
        barrier.wait()
        result = store.claim_job(f"worker-{index}", 30)
        if result:
            with lock:
                claims.append(result)

    threads = [Thread(target=claim, args=(index,)) for index in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claims) == 1
    assert claims[0].id == job.id
    assert claims[0].fencing_token == 1


def test_expired_lease_is_recovered_and_stale_fencing_write_is_rejected(tmp_path: Path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'fence.db'}")
    store.initialize()
    run = RunRecord(id="RUN-FENCE", incident=incident())
    store.save_run(run)
    store.enqueue_job(run.id, "start")
    first = store.claim_job("worker-old", 0)
    assert first is not None
    second = store.claim_job("worker-new", 30)
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1
    stale_run = store.get_run(run.id)
    assert stale_run is not None
    stale_run.diagnosis = "stale worker must not persist"
    with pytest.raises(LeaseLostError):
        store.save_run_with_lease(
            stale_run, first.id, "worker-old", first.fencing_token
        )
    current = store.get_run(run.id)
    assert current is not None
    current.diagnosis = "new owner"
    store.save_run_with_lease(
        current, second.id, "worker-new", second.fencing_token
    )
    assert store.get_run(run.id).diagnosis == "new owner"


def test_run_and_job_are_committed_atomically(tmp_path: Path) -> None:
    database = tmp_path / "atomic.db"
    store = SQLiteStore(f"sqlite:///{database}")
    store.initialize()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            """
            CREATE TRIGGER reject_job_insert
            BEFORE INSERT ON agent_jobs
            BEGIN
                SELECT RAISE(ABORT, 'simulated job insert failure');
            END;
            """
        )
    run = RunRecord(id="RUN-ATOMIC", incident=incident())

    with pytest.raises(sqlite3.IntegrityError, match="simulated job insert failure"):
        store.save_run_and_enqueue(run, "start")

    assert run.version == 0
    assert store.get_run(run.id) is None
    assert store.list_jobs(run.id) == []


def test_enqueue_is_idempotent_for_active_run_stage(tmp_path: Path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'dedupe.db'}")
    store.initialize()
    run = RunRecord(id="RUN-DEDUPE", incident=incident())
    store.save_run(run)

    first = store.enqueue_job(run.id, "start")
    second = store.enqueue_job(run.id, "start")

    assert second.id == first.id
    assert len(store.list_jobs(run.id)) == 1


def test_reconciler_repairs_only_queued_orphans(tmp_path: Path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'reconcile.db'}")
    store.initialize()
    initial = RunRecord(id="RUN-ORPHAN-START", incident=incident())
    approved = RunRecord(
        id="RUN-ORPHAN-RESUME",
        incident=incident(),
        current_node="execute",
        approval=Approval(required=True, decision="approved"),
    )
    waiting = RunRecord(
        id="RUN-NO-REPAIR",
        incident=incident(),
        status="awaiting_approval",
        current_node="gate",
    )
    for run in (initial, approved, waiting):
        store.save_run(run)

    repaired = store.reconcile_orphaned_runs()

    assert {(job.run_id, job.stage) for job in repaired} == {
        (initial.id, "start"),
        (approved.id, "resume"),
    }
    assert store.reconcile_orphaned_runs() == []
    assert store.list_jobs(waiting.id) == []


def test_job_lease_lifecycle_heartbeats_requeues_and_finishes(tmp_path: Path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'job-lifecycle.db'}")
    store.initialize()
    run = RunRecord(id="RUN-JOB-LIFECYCLE", incident=incident())
    store.save_run_and_enqueue(run, "start")

    first = store.claim_job("worker-a", 30)
    assert first is not None
    store.assert_job_lease(first.id, "worker-a", first.fencing_token)
    renewed = store.heartbeat_job(first.id, "worker-a", first.fencing_token, 30)
    assert renewed.lease_until is not None
    with pytest.raises(ValueError, match="terminal"):
        store.finish_job(
            first.id, "worker-a", first.fencing_token, JobStatus.QUEUED
        )

    queued = store.requeue_job(
        first.id, "worker-a", first.fencing_token, 0, "retryable failure"
    )
    assert queued.status == "queued"
    assert queued.owner is None
    assert queued.last_error == "retryable failure"

    second = store.claim_job("worker-b", 30)
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1
    finished = store.finish_job(
        second.id, "worker-b", second.fencing_token, JobStatus.SUCCEEDED
    )
    assert finished.status == "succeeded"
    assert store.get_job(second.id).status == "succeeded"
    assert store.get_job("JOB-DOES-NOT-EXIST") is None
    assert [job.id for job in store.list_jobs()] == [second.id]

    with pytest.raises(LeaseLostError):
        store.heartbeat_job(first.id, "worker-a", first.fencing_token, 30)
    with pytest.raises(LeaseLostError):
        store.assert_job_lease(first.id, "worker-a", first.fencing_token)
