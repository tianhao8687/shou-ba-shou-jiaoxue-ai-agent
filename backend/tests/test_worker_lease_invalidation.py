from __future__ import annotations

import time

from app.schemas import JobRecord, JobStatus
from app.store import LeaseLostError
from app.worker import AgentWorker


class HeartbeatFailingStore:
    def __init__(self) -> None:
        self.job = JobRecord(id="JOB-LEASE-LOSS", run_id="RUN-LEASE-LOSS", stage="start")
        self.job.status = "claimed"
        self.job.owner = "worker-old"
        self.job.fencing_token = 1
        self.claimed = False
        self.finished = False
        self.requeued = False

    def reconcile_orphaned_runs(self):
        return []

    def claim_job(self, worker_id: str, lease_seconds: float):
        del worker_id, lease_seconds
        if self.claimed:
            return None
        self.claimed = True
        return self.job

    def heartbeat_job(self, *args, **kwargs):
        del args, kwargs
        raise OSError("database heartbeat channel unavailable")

    def finish_job(self, *args, **kwargs):
        del args, kwargs
        self.finished = True

    def requeue_job(self, *args, **kwargs):
        del args, kwargs
        self.requeued = True


class LeaseAwareEngine:
    def __init__(self) -> None:
        self.aborted_before_side_effect = False
        self.side_effect = False

    def process(self, run_id: str, lease) -> None:
        del run_id
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                lease.assert_active()
            except LeaseLostError:
                self.aborted_before_side_effect = True
                raise
            time.sleep(0.002)
        self.side_effect = True


def test_repeated_heartbeat_failures_actively_invalidate_running_engine() -> None:
    store = HeartbeatFailingStore()
    engine = LeaseAwareEngine()
    worker = AgentWorker(
        store,  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
        worker_id="worker-old",
        lease_seconds=0.2,
        heartbeat_seconds=0.01,
        heartbeat_failure_limit=2,
    )

    assert worker.run_once() is True

    assert engine.aborted_before_side_effect is True
    assert engine.side_effect is False
    assert worker.lease_losses == 1
    assert store.finished is False
    assert store.requeued is False


class RecordingStore:
    def __init__(self, *, attempts: int = 1, has_job: bool = True) -> None:
        self.job = (
            JobRecord(
                id=f"JOB-FAIL-{attempts}",
                run_id="RUN-WORKER-FAIL",
                stage="retry",
                status="claimed",
                owner="worker-test",
                fencing_token=3,
                attempts=attempts,
            )
            if has_job
            else None
        )
        self.claimed = False
        self.finished_status = None
        self.requeued = False
        self.registry_heartbeats = 0
        self.registry_removed = False

    def reconcile_orphaned_runs(self):
        return []

    def claim_job(self, worker_id: str, lease_seconds: float):
        del worker_id, lease_seconds
        if self.claimed:
            return None
        self.claimed = True
        return self.job

    def heartbeat_job(self, *args, **kwargs):
        del args, kwargs
        return self.job

    def finish_job(self, job_id, worker_id, fencing_token, status, error=None):
        del job_id, worker_id, fencing_token, error
        self.finished_status = status
        return self.job

    def requeue_job(self, *args, **kwargs):
        del args, kwargs
        self.requeued = True
        return self.job

    def heartbeat_worker(self, worker_id, metadata=None):
        del worker_id, metadata
        self.registry_heartbeats += 1

    def remove_worker(self, worker_id):
        del worker_id
        self.registry_removed = True


class FailingEngine:
    def process(self, run_id: str, lease) -> None:
        del run_id, lease
        raise RuntimeError("simulated engine failure")


def test_worker_requeues_retryable_engine_failure() -> None:
    store = RecordingStore(attempts=1)
    worker = AgentWorker(
        store,  # type: ignore[arg-type]
        FailingEngine(),  # type: ignore[arg-type]
        worker_id="worker-test",
        lease_seconds=1,
        heartbeat_seconds=0.1,
    )

    assert worker.run_once() is True
    assert worker.failed_jobs == 1
    assert store.requeued is True
    assert store.finished_status is None
    assert "simulated engine failure" in (worker.last_error or "")


def test_worker_marks_job_failed_after_retry_budget() -> None:
    store = RecordingStore(attempts=3)
    worker = AgentWorker(
        store,  # type: ignore[arg-type]
        FailingEngine(),  # type: ignore[arg-type]
        worker_id="worker-test",
        lease_seconds=1,
        heartbeat_seconds=0.1,
    )

    assert worker.run_once() is True
    assert store.requeued is False
    assert store.finished_status == JobStatus.FAILED


def test_worker_background_lifecycle_is_observable_when_queue_is_empty() -> None:
    store = RecordingStore(has_job=False)
    worker = AgentWorker(
        store,  # type: ignore[arg-type]
        FailingEngine(),  # type: ignore[arg-type]
        worker_id="worker-idle",
        lease_seconds=1,
        heartbeat_seconds=0.1,
        poll_seconds=0.01,
    )

    assert worker.run_once() is False
    worker.start()
    assert worker.health()["status"] == "running"
    worker.start()
    worker.stop()
    assert worker.health()["status"] == "stopped"
    assert store.registry_heartbeats >= 1
    assert store.registry_removed is True
