from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from ..migrations import run_sqlite_migrations, sqlite_schema_status
from ..schemas import EvaluationReport, JobRecord, JobStatus, RunRecord, utc_now
from .base import ConcurrencyError, LeaseLostError, Store
from .jobs import (
    ACTIVE_JOB_STATUSES,
    JOB_COLUMNS,
    _job_from_row,
    _new_job,
    _recovery_stage,
)
from .runs import _parse_datetime, _with_version

class SQLiteStore(Store):
    def __init__(self, database_url: str) -> None:
        raw_path = database_url.removeprefix("sqlite:///")
        self.path = Path(raw_path).resolve()
        self._model_slot_lock = threading.Lock()

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def model_inference_slot(self):
        """Serialize local-model calls in the single-process SQLite runtime."""
        with self._model_slot_lock:
            yield

    def initialize(self) -> None:
        with self._connect() as connection:
            run_sqlite_migrations(connection)

    def ping(self) -> None:
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()

    def schema_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            return sqlite_schema_status(connection)

    def heartbeat_worker(
        self, worker_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        now = utc_now().isoformat()
        encoded = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO worker_instances(worker_id, started_at, last_seen_at, metadata)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at,
                    metadata=excluded.metadata
                """,
                (worker_id, now, now, encoded),
            )

    def list_live_workers(self, max_age_seconds: float) -> list[dict[str, Any]]:
        cutoff = (utc_now() - timedelta(seconds=max(0.0, max_age_seconds))).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT worker_id, started_at, last_seen_at, metadata "
                "FROM worker_instances WHERE last_seen_at >= ? ORDER BY worker_id",
                (cutoff,),
            ).fetchall()
        return [
            {
                "worker_id": str(row["worker_id"]),
                "started_at": _parse_datetime(row["started_at"]),
                "last_seen_at": _parse_datetime(row["last_seen_at"]),
                "metadata": json.loads(row["metadata"]),
            }
            for row in rows
        ]

    def remove_worker(self, worker_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM worker_instances WHERE worker_id = ?", (worker_id,)
            )

    @staticmethod
    def _save_run_in_transaction(
        connection: sqlite3.Connection, run: RunRecord
    ) -> tuple[int, datetime]:
        expected = run.version
        next_version = expected + 1
        candidate = run.model_copy(update={"version": next_version, "updated_at": utc_now()})
        row = connection.execute(
            "SELECT version FROM agent_runs WHERE id = ?", (run.id,)
        ).fetchone()
        if row is None:
            if expected != 0:
                raise ConcurrencyError(f"运行 {run.id} 已被删除或版本失效")
            connection.execute(
                "INSERT INTO agent_runs(id, payload, updated_at, version) VALUES (?, ?, ?, ?)",
                (run.id, candidate.model_dump_json(), candidate.updated_at.isoformat(), next_version),
            )
        else:
            current = int(row["version"])
            if current != expected:
                raise ConcurrencyError(f"运行 {run.id} 版本冲突：期望 {expected}，当前 {current}")
            cursor = connection.execute(
                "UPDATE agent_runs SET payload=?, updated_at=?, version=? WHERE id=? AND version=?",
                (
                    candidate.model_dump_json(),
                    candidate.updated_at.isoformat(),
                    next_version,
                    run.id,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyError(f"运行 {run.id} 在保存期间被其他请求更新")
        return next_version, candidate.updated_at

    @staticmethod
    def _active_job_in_transaction(
        connection: sqlite3.Connection, run_id: str, stage: str
    ) -> JobRecord | None:
        row = connection.execute(
            f"""
            SELECT {JOB_COLUMNS} FROM agent_jobs
            WHERE run_id=? AND stage=? AND status IN (?, ?)
            ORDER BY created_at LIMIT 1
            """,
            (run_id, stage, *ACTIVE_JOB_STATUSES),
        ).fetchone()
        return _job_from_row(row) if row else None

    @staticmethod
    def _insert_job_in_transaction(
        connection: sqlite3.Connection, run_id: str, stage: str
    ) -> JobRecord:
        existing = SQLiteStore._active_job_in_transaction(connection, run_id, stage)
        if existing is not None:
            return existing
        job = _new_job(run_id, stage)
        connection.execute(
            """
            INSERT INTO agent_jobs(
                id, run_id, stage, status, owner, lease_until, fencing_token, attempts,
                available_at, created_at, updated_at, last_error
            ) VALUES (?, ?, ?, ?, NULL, NULL, 0, 0, ?, ?, ?, NULL)
            """,
            (
                job.id,
                run_id,
                stage,
                JobStatus.QUEUED,
                job.available_at.isoformat(),
                job.created_at.isoformat(),
                job.updated_at.isoformat(),
            ),
        )
        return job

    def save_run(self, run: RunRecord) -> RunRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            next_version, updated_at = self._save_run_in_transaction(connection, run)
        run.version = next_version
        run.updated_at = updated_at
        return run

    def save_run_and_enqueue(
        self, run: RunRecord, stage: str
    ) -> tuple[RunRecord, JobRecord]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            next_version, updated_at = self._save_run_in_transaction(connection, run)
            job = self._insert_job_in_transaction(connection, run.id, stage)
        run.version = next_version
        run.updated_at = updated_at
        return run, job

    def save_run_with_lease(
        self, run: RunRecord, job_id: str, worker_id: str, fencing_token: int
    ) -> RunRecord:
        expected = run.version
        next_version = expected + 1
        candidate = run.model_copy(update={"version": next_version, "updated_at": utc_now()})
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                """
                SELECT 1 FROM agent_jobs
                WHERE id=? AND run_id=? AND status=? AND owner=? AND fencing_token=? AND lease_until>?
                """,
                (job_id, run.id, JobStatus.CLAIMED, worker_id, fencing_token, now),
            ).fetchone()
            if lease is None:
                raise LeaseLostError(f"job {job_id} fencing token is stale")
            row = connection.execute(
                "SELECT version FROM agent_runs WHERE id=?", (run.id,)
            ).fetchone()
            if row is None or int(row["version"]) != expected:
                current = "missing" if row is None else int(row["version"])
                raise ConcurrencyError(
                    f"运行 {run.id} 版本冲突：期望 {expected}，当前 {current}"
                )
            cursor = connection.execute(
                "UPDATE agent_runs SET payload=?,updated_at=?,version=? WHERE id=? AND version=?",
                (
                    candidate.model_dump_json(),
                    candidate.updated_at.isoformat(),
                    next_version,
                    run.id,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyError(f"运行 {run.id} 在保存期间被其他请求更新")
        run.version = next_version
        run.updated_at = candidate.updated_at
        return run

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload, version FROM agent_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _with_version(row["payload"], int(row["version"])) if row else None

    def list_runs(
        self, tenant_id: str | None = None, limit: int | None = None
    ) -> list[RunRecord]:
        query = "SELECT payload, version FROM agent_runs"
        parameters: list[str | int] = []
        if tenant_id is not None:
            query += " WHERE json_extract(payload, '$.tenant_id') = ?"
            parameters.append(tenant_id)
        query += " ORDER BY updated_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [_with_version(row["payload"], int(row["version"])) for row in rows]

    def enqueue_job(self, run_id: str, stage: str) -> JobRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM agent_runs WHERE id=?", (run_id,)
            ).fetchone() is None:
                raise ValueError(f"run {run_id} does not exist")
            return self._insert_job_in_transaction(connection, run_id, stage)

    def reconcile_orphaned_runs(self) -> list[JobRecord]:
        repaired: list[JobRecord] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT payload, version FROM agent_runs ORDER BY updated_at"
            ).fetchall()
            for row in rows:
                run = _with_version(row["payload"], int(row["version"]))
                if run.status != "queued":
                    continue
                active = connection.execute(
                    """
                    SELECT 1 FROM agent_jobs
                    WHERE run_id=? AND status IN (?, ?) LIMIT 1
                    """,
                    (run.id, *ACTIVE_JOB_STATUSES),
                ).fetchone()
                if active is None:
                    repaired.append(
                        self._insert_job_in_transaction(
                            connection, run.id, _recovery_stage(run)
                        )
                    )
        return repaired

    def claim_job(self, worker_id: str, lease_seconds: int) -> JobRecord | None:
        now = utc_now()
        deadline = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT {JOB_COLUMNS} FROM agent_jobs
                WHERE (status = ? AND available_at <= ?)
                   OR (status = ? AND lease_until IS NOT NULL AND lease_until <= ?)
                ORDER BY CASE WHEN status = ? THEN 0 ELSE 1 END, available_at, created_at
                LIMIT 1
                """,
                (
                    JobStatus.QUEUED,
                    now.isoformat(),
                    JobStatus.CLAIMED,
                    now.isoformat(),
                    JobStatus.CLAIMED,
                ),
            ).fetchone()
            if row is None:
                return None
            next_token = int(row["fencing_token"]) + 1
            connection.execute(
                """
                UPDATE agent_jobs
                SET status=?, owner=?, lease_until=?, fencing_token=?, attempts=attempts+1,
                    updated_at=?, last_error=CASE WHEN status=? THEN 'lease expired; recovered' ELSE last_error END
                WHERE id=?
                """,
                (
                    JobStatus.CLAIMED,
                    worker_id,
                    deadline.isoformat(),
                    next_token,
                    now.isoformat(),
                    JobStatus.CLAIMED,
                    row["id"],
                ),
            )
            claimed = connection.execute(
                f"SELECT {JOB_COLUMNS} FROM agent_jobs WHERE id=?", (row["id"],)
            ).fetchone()
        return _job_from_row(claimed)

    def heartbeat_job(
        self, job_id: str, worker_id: str, fencing_token: int, lease_seconds: int
    ) -> JobRecord:
        now = utc_now()
        deadline = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_jobs SET lease_until=?, updated_at=?
                WHERE id=? AND status=? AND owner=? AND fencing_token=? AND lease_until>?
                """,
                (
                    deadline.isoformat(),
                    now.isoformat(),
                    job_id,
                    JobStatus.CLAIMED,
                    worker_id,
                    fencing_token,
                    now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"job {job_id} lease has been lost")
        result = self.get_job(job_id)
        if result is None:
            raise LeaseLostError(f"job {job_id} disappeared")
        return result

    def assert_job_lease(
        self, job_id: str, worker_id: str, fencing_token: int
    ) -> None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM agent_jobs
                WHERE id=? AND status=? AND owner=? AND fencing_token=? AND lease_until>?
                """,
                (
                    job_id,
                    JobStatus.CLAIMED,
                    worker_id,
                    fencing_token,
                    utc_now().isoformat(),
                ),
            ).fetchone()
        if row is None:
            raise LeaseLostError(f"job {job_id} lease has been lost")

    def finish_job(
        self,
        job_id: str,
        worker_id: str,
        fencing_token: int,
        status: JobStatus,
        error: str | None = None,
    ) -> JobRecord:
        if status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
            raise ValueError("finish status must be terminal")
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_jobs SET status=?, lease_until=NULL, updated_at=?, last_error=?
                WHERE id=? AND status=? AND owner=? AND fencing_token=?
                """,
                (status, now.isoformat(), error, job_id, JobStatus.CLAIMED, worker_id, fencing_token),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"job {job_id} cannot be finished by stale worker")
        result = self.get_job(job_id)
        assert result is not None
        return result

    def requeue_job(
        self,
        job_id: str,
        worker_id: str,
        fencing_token: int,
        delay_seconds: float,
        error: str,
    ) -> JobRecord:
        now = utc_now()
        available = now + timedelta(seconds=delay_seconds)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_jobs SET status=?, owner=NULL, lease_until=NULL, available_at=?,
                    updated_at=?, last_error=?
                WHERE id=? AND status=? AND owner=? AND fencing_token=?
                """,
                (
                    JobStatus.QUEUED,
                    available.isoformat(),
                    now.isoformat(),
                    error,
                    job_id,
                    JobStatus.CLAIMED,
                    worker_id,
                    fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise LeaseLostError(f"job {job_id} cannot be requeued by stale worker")
        result = self.get_job(job_id)
        assert result is not None
        return result

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {JOB_COLUMNS} FROM agent_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return _job_from_row(row) if row else None

    def list_jobs(self, run_id: str | None = None) -> list[JobRecord]:
        with self._connect() as connection:
            if run_id:
                rows = connection.execute(
                    f"SELECT {JOB_COLUMNS} FROM agent_jobs WHERE run_id=? ORDER BY created_at DESC",
                    (run_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    f"SELECT {JOB_COLUMNS} FROM agent_jobs ORDER BY created_at DESC"
                ).fetchall()
        return [_job_from_row(row) for row in rows]

    def save_evaluation(self, report: EvaluationReport) -> EvaluationReport:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO evaluation_reports(id, tenant_id, payload, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET tenant_id=excluded.tenant_id,
                    payload=excluded.payload, created_at=excluded.created_at
                """,
                (
                    report.id,
                    report.tenant_id,
                    report.model_dump_json(),
                    report.created_at.isoformat(),
                ),
            )
        return report

    def latest_evaluation(self, tenant_id: str) -> EvaluationReport | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM evaluation_reports WHERE tenant_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
        return EvaluationReport.model_validate_json(row["payload"]) if row else None
