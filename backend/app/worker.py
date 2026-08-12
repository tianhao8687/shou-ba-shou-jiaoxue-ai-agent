from __future__ import annotations

import os
import socket
import threading
import time
from typing import Any

from .agent import AgentEngine, LeaseContext
from .schemas import JobStatus
from .store import LeaseLostError, Store


class AgentWorker:
    def __init__(
        self,
        store: Store,
        engine: AgentEngine,
        *,
        worker_id: str,
        lease_seconds: float = 30,
        heartbeat_seconds: float = 8,
        heartbeat_failure_limit: int = 2,
        registry_heartbeat_seconds: float = 5.0,
        poll_seconds: float = 0.25,
        reconcile_seconds: float = 30.0,
    ) -> None:
        if heartbeat_seconds >= lease_seconds:
            raise ValueError("heartbeat interval must be shorter than the lease")
        if heartbeat_failure_limit < 1:
            raise ValueError("heartbeat failure limit must be positive")
        if registry_heartbeat_seconds <= 0:
            raise ValueError("registry heartbeat interval must be positive")
        self.store = store
        self.engine = engine
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.heartbeat_failure_limit = heartbeat_failure_limit
        self.registry_heartbeat_seconds = registry_heartbeat_seconds
        self.poll_seconds = poll_seconds
        self.reconcile_seconds = max(1.0, reconcile_seconds)
        self._next_reconcile_at = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._registry_stop = threading.Event()
        self._registry_thread: threading.Thread | None = None
        self.processed_jobs = 0
        self.recovered_jobs = 0
        self.failed_jobs = 0
        self.lease_losses = 0
        self.reconciled_jobs = 0
        self.last_error: str | None = None

    def _registry_metadata(self) -> dict[str, Any]:
        return {
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "lease_seconds": self.lease_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
        }

    def start_registry(self) -> None:
        """Advertise process liveness independently of whether a job is active."""
        if self._registry_thread and self._registry_thread.is_alive():
            return
        self._registry_stop.clear()
        self.store.heartbeat_worker(self.worker_id, self._registry_metadata())

        def heartbeat_registry() -> None:
            while not self._registry_stop.wait(self.registry_heartbeat_seconds):
                try:
                    self.store.heartbeat_worker(self.worker_id, self._registry_metadata())
                except Exception as exc:
                    # An expired registry row makes the control plane report the
                    # worker as unavailable; never preserve a false green state.
                    self.last_error = f"registry heartbeat {type(exc).__name__}: {exc}"

        self._registry_thread = threading.Thread(
            target=heartbeat_registry,
            name=f"{self.worker_id}-registry",
            daemon=True,
        )
        self._registry_thread.start()

    def stop_registry(self) -> None:
        self._registry_stop.set()
        if self._registry_thread:
            self._registry_thread.join(timeout=1.0)
        try:
            self.store.remove_worker(self.worker_id)
        except Exception as exc:
            self.last_error = f"registry removal {type(exc).__name__}: {exc}"

    def _reconcile_if_due(self) -> None:
        monotonic_now = time.monotonic()
        if monotonic_now < self._next_reconcile_at:
            return
        self._next_reconcile_at = monotonic_now + self.reconcile_seconds
        try:
            self.reconciled_jobs += len(self.store.reconcile_orphaned_runs())
        except Exception as exc:
            # Existing jobs must remain processable even if a repair scan has a
            # transient database failure. The next bounded interval retries it.
            self.last_error = f"reconcile {type(exc).__name__}: {exc}"

    def run_once(self) -> bool:
        # Claim durable work before the repair scan. Besides reducing queue latency,
        # this makes the persisted claim observable before process() reads Run state;
        # crash recovery can therefore fence and reclaim that exact boundary.
        job = self.store.claim_job(self.worker_id, self.lease_seconds)
        if job is None:
            self._reconcile_if_due()
            return False
        recovered = job.attempts > 1
        if recovered:
            self.recovered_jobs += 1
        heartbeat_stop = threading.Event()
        lease_lost = threading.Event()

        def heartbeat() -> None:
            consecutive_failures = 0
            while not heartbeat_stop.wait(self.heartbeat_seconds):
                try:
                    self.store.heartbeat_job(
                        job.id, self.worker_id, job.fencing_token, self.lease_seconds
                    )
                    consecutive_failures = 0
                except LeaseLostError:
                    lease_lost.set()
                    return
                except Exception as exc:  # A transient heartbeat error must not silently extend ownership.
                    consecutive_failures += 1
                    self.last_error = f"heartbeat {type(exc).__name__}: {exc}"
                    if consecutive_failures >= self.heartbeat_failure_limit:
                        lease_lost.set()
                        return

        heartbeat_thread = threading.Thread(
            target=heartbeat, name=f"{self.worker_id}-heartbeat", daemon=True
        )
        heartbeat_thread.start()
        try:
            self.engine.process(
                job.run_id,
                LeaseContext(
                    job_id=job.id,
                    worker_id=self.worker_id,
                    fencing_token=job.fencing_token,
                    recovered=recovered,
                    lost_event=lease_lost,
                ),
            )
            if lease_lost.is_set():
                raise LeaseLostError(f"job {job.id} lease was lost during execution")
            self.store.finish_job(
                job.id,
                self.worker_id,
                job.fencing_token,
                JobStatus.SUCCEEDED,
            )
            self.processed_jobs += 1
            return True
        except LeaseLostError:
            # A newer fencing token owns the job. The stale worker must not mutate job or run state.
            self.lease_losses += 1
            return True
        except Exception as exc:
            self.failed_jobs += 1
            self.last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
            try:
                if job.attempts < 3:
                    self.store.requeue_job(
                        job.id,
                        self.worker_id,
                        job.fencing_token,
                        min(2 ** job.attempts, 8),
                        self.last_error,
                    )
                else:
                    self.store.finish_job(
                        job.id,
                        self.worker_id,
                        job.fencing_token,
                        JobStatus.FAILED,
                        self.last_error,
                    )
            except LeaseLostError:
                self.lease_losses += 1
            return True
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1.0)
            self._reconcile_if_due()

    def run_forever(self) -> None:
        self.start_registry()
        try:
            while not self._stop.is_set():
                processed = self.run_once()
                if not processed:
                    self._stop.wait(self.poll_seconds)
        finally:
            self.stop_registry()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_forever, name=self.worker_id, daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def health(self) -> dict[str, Any]:
        return {
            "status": "running" if self._thread and self._thread.is_alive() else "stopped",
            "worker_id": self.worker_id,
            "processed_jobs": self.processed_jobs,
            "recovered_jobs": self.recovered_jobs,
            "failed_jobs": self.failed_jobs,
            "lease_losses": self.lease_losses,
            "reconciled_jobs": self.reconciled_jobs,
            "registry_running": bool(
                self._registry_thread and self._registry_thread.is_alive()
            ),
            "last_error": self.last_error,
        }
