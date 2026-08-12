from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from typing import Any

from ..schemas import EvaluationReport, JobRecord, JobStatus, RunRecord

class ConcurrencyError(RuntimeError):
    pass


class LeaseLostError(ConcurrencyError):
    pass


class Store(ABC):
    def close(self) -> None:
        """Release process-scoped resources; stateless stores need no action."""

    @abstractmethod
    def initialize(self) -> None: ...

    @abstractmethod
    def ping(self) -> None:
        """Raise when the durable store cannot serve a trivial query."""
        ...

    @abstractmethod
    def schema_status(self) -> dict[str, Any]: ...

    @abstractmethod
    def heartbeat_worker(
        self, worker_id: str, metadata: dict[str, Any] | None = None
    ) -> None: ...

    @abstractmethod
    def list_live_workers(self, max_age_seconds: float) -> list[dict[str, Any]]: ...

    @abstractmethod
    def remove_worker(self, worker_id: str) -> None: ...

    @abstractmethod
    def save_run(self, run: RunRecord) -> RunRecord: ...

    @abstractmethod
    def save_run_and_enqueue(
        self, run: RunRecord, stage: str
    ) -> tuple[RunRecord, JobRecord]:
        """CAS-save a run and create its durable job in the same transaction."""
        ...

    @abstractmethod
    def save_run_with_lease(
        self, run: RunRecord, job_id: str, worker_id: str, fencing_token: int
    ) -> RunRecord: ...

    @abstractmethod
    def get_run(self, run_id: str) -> RunRecord | None: ...

    @abstractmethod
    def list_runs(
        self, tenant_id: str | None = None, limit: int | None = None
    ) -> list[RunRecord]: ...

    @abstractmethod
    def enqueue_job(self, run_id: str, stage: str) -> JobRecord: ...

    @abstractmethod
    def reconcile_orphaned_runs(self) -> list[JobRecord]:
        """Repair queued runs that have no queued or claimed durable job."""
        ...

    @abstractmethod
    def claim_job(self, worker_id: str, lease_seconds: int) -> JobRecord | None: ...

    @abstractmethod
    def heartbeat_job(
        self, job_id: str, worker_id: str, fencing_token: int, lease_seconds: int
    ) -> JobRecord: ...

    @abstractmethod
    def assert_job_lease(
        self, job_id: str, worker_id: str, fencing_token: int
    ) -> None: ...

    @abstractmethod
    def finish_job(
        self,
        job_id: str,
        worker_id: str,
        fencing_token: int,
        status: JobStatus,
        error: str | None = None,
    ) -> JobRecord: ...

    @abstractmethod
    def requeue_job(
        self,
        job_id: str,
        worker_id: str,
        fencing_token: int,
        delay_seconds: float,
        error: str,
    ) -> JobRecord: ...

    @abstractmethod
    def get_job(self, job_id: str) -> JobRecord | None: ...

    @abstractmethod
    def list_jobs(self, run_id: str | None = None) -> list[JobRecord]: ...

    @abstractmethod
    def save_evaluation(self, report: EvaluationReport) -> EvaluationReport: ...

    @abstractmethod
    def latest_evaluation(self, tenant_id: str) -> EvaluationReport | None: ...

    @abstractmethod
    def model_inference_slot(self) -> AbstractContextManager[None]: ...
