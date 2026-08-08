from pathlib import Path
from threading import Barrier, Lock, Thread

import pytest

from app.schemas import Incident, RunRecord
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
