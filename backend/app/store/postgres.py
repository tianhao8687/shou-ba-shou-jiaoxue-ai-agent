from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import threading
from typing import Any

from ..migrations import postgres_schema_status, run_postgres_migrations
from ..schemas import EvaluationReport, JobRecord, JobStatus, RunRecord, utc_now
from .base import ConcurrencyError, LeaseLostError, Store
from .jobs import (
    ACTIVE_JOB_STATUSES,
    JOB_COLUMNS,
    POSTGRES_JOB_COLUMNS_FROM_UPDATE,
    _job_from_row,
    _new_job,
    _recovery_stage,
)
from .runs import _parse_datetime, _with_version

class PostgresStore(Store):
    def __init__(
        self,
        database_url: str,
        *,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        pool_timeout_seconds: float = 10.0,
        pool_factory=None,
    ) -> None:
        if pool_min_size < 0 or pool_max_size < 1:
            raise ValueError("PostgreSQL pool sizes must be positive and bounded")
        if pool_min_size > pool_max_size:
            raise ValueError("PostgreSQL pool min size cannot exceed max size")
        if pool_timeout_seconds <= 0:
            raise ValueError("PostgreSQL pool timeout must be positive")
        self.database_url = database_url
        if pool_factory is None:
            from psycopg_pool import ConnectionPool

            pool_factory = ConnectionPool
        self._pool_timeout_seconds = pool_timeout_seconds
        self._pool = pool_factory(
            conninfo=database_url,
            min_size=pool_min_size,
            max_size=pool_max_size,
            timeout=pool_timeout_seconds,
            open=False,
            name="harbor-agentops-store",
        )
        self._pool_opened = False
        self._pool_closed = False
        self._pool_lifecycle_lock = threading.Lock()

    def _connect(self):
        self._ensure_pool_open()
        return self._pool.connection(timeout=self._pool_timeout_seconds)

    def _ensure_pool_open(self, *, wait: bool = False) -> None:
        with self._pool_lifecycle_lock:
            if self._pool_closed:
                raise RuntimeError("PostgreSQL connection pool is closed")
            if not self._pool_opened:
                self._pool.open(
                    wait=wait,
                    timeout=self._pool_timeout_seconds,
                )
                self._pool_opened = True
            elif wait:
                self._pool.wait(timeout=self._pool_timeout_seconds)

    def close(self) -> None:
        with self._pool_lifecycle_lock:
            if self._pool_closed:
                return
            self._pool_closed = True
            if self._pool_opened:
                self._pool.close(timeout=self._pool_timeout_seconds)

    @contextmanager
    def model_inference_slot(self):
        """Coordinate one CPU-model slot across replicas; crashes release the session lock."""
        lock_sql = (
            "hashtextextended(current_database() || ':harbor-agentops:model-inference', 0)"
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT pg_advisory_lock({lock_sql})")
            try:
                yield
            finally:
                cursor.execute(f"SELECT pg_advisory_unlock({lock_sql})")

    def initialize(self) -> None:
        self._ensure_pool_open(wait=True)
        with self._connect() as connection:
            run_postgres_migrations(connection)

    def ping(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()

    def schema_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            return postgres_schema_status(connection)

    def heartbeat_worker(
        self, worker_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        from psycopg.types.json import Jsonb

        now = utc_now()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO worker_instances(worker_id, started_at, last_seen_at, metadata)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(worker_id) DO UPDATE SET
                    last_seen_at=EXCLUDED.last_seen_at,
                    metadata=EXCLUDED.metadata
                """,
                (worker_id, now, now, Jsonb(metadata or {})),
            )

    def list_live_workers(self, max_age_seconds: float) -> list[dict[str, Any]]:
        cutoff = utc_now() - timedelta(seconds=max(0.0, max_age_seconds))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT worker_id, started_at, last_seen_at, metadata "
                "FROM worker_instances WHERE last_seen_at >= %s ORDER BY worker_id",
                (cutoff,),
            )
            rows = cursor.fetchall()
        return [
            {
                "worker_id": str(row[0]),
                "started_at": _parse_datetime(row[1]),
                "last_seen_at": _parse_datetime(row[2]),
                "metadata": dict(row[3] or {}),
            }
            for row in rows
        ]

    def remove_worker(self, worker_id: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM worker_instances WHERE worker_id = %s", (worker_id,)
            )

    @staticmethod
    def _save_run_in_transaction(cursor, run: RunRecord) -> tuple[int, datetime]:
        from psycopg.types.json import Jsonb

        expected = run.version
        next_version = expected + 1
        candidate = run.model_copy(update={"version": next_version, "updated_at": utc_now()})
        cursor.execute("SELECT version FROM agent_runs WHERE id=%s FOR UPDATE", (run.id,))
        row = cursor.fetchone()
        if row is None:
            if expected != 0:
                raise ConcurrencyError(f"运行 {run.id} 已被删除或版本失效")
            cursor.execute(
                "INSERT INTO agent_runs(id,payload,updated_at,version) VALUES (%s,%s,%s,%s)",
                (run.id, Jsonb(candidate.model_dump(mode="json")), candidate.updated_at, next_version),
            )
        else:
            if int(row[0]) != expected:
                raise ConcurrencyError(f"运行 {run.id} 版本冲突：期望 {expected}，当前 {row[0]}")
            cursor.execute(
                "UPDATE agent_runs SET payload=%s,updated_at=%s,version=%s WHERE id=%s AND version=%s",
                (Jsonb(candidate.model_dump(mode="json")), candidate.updated_at, next_version, run.id, expected),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyError(f"运行 {run.id} 在保存期间被其他请求更新")
        return next_version, candidate.updated_at

    @staticmethod
    def _active_job_in_transaction(cursor, run_id: str, stage: str) -> JobRecord | None:
        cursor.execute(
            f"""
            SELECT {JOB_COLUMNS} FROM agent_jobs
            WHERE run_id=%s AND stage=%s AND status IN (%s, %s)
            ORDER BY created_at LIMIT 1
            """,
            (run_id, stage, *ACTIVE_JOB_STATUSES),
        )
        row = cursor.fetchone()
        return _job_from_row(row) if row else None

    @staticmethod
    def _insert_job_in_transaction(cursor, run_id: str, stage: str) -> JobRecord:
        existing = PostgresStore._active_job_in_transaction(cursor, run_id, stage)
        if existing is not None:
            return existing
        job = _new_job(run_id, stage)
        cursor.execute(
            """
            INSERT INTO agent_jobs(id,run_id,stage,status,owner,lease_until,fencing_token,attempts,
                available_at,created_at,updated_at,last_error)
            VALUES (%s,%s,%s,%s,NULL,NULL,0,0,%s,%s,%s,NULL)
            """,
            (
                job.id,
                run_id,
                stage,
                JobStatus.QUEUED,
                job.available_at,
                job.created_at,
                job.updated_at,
            ),
        )
        return job

    def save_run(self, run: RunRecord) -> RunRecord:
        with self._connect() as connection, connection.cursor() as cursor:
            next_version, updated_at = self._save_run_in_transaction(cursor, run)
        run.version = next_version
        run.updated_at = updated_at
        return run

    def save_run_and_enqueue(
        self, run: RunRecord, stage: str
    ) -> tuple[RunRecord, JobRecord]:
        with self._connect() as connection, connection.cursor() as cursor:
            next_version, updated_at = self._save_run_in_transaction(cursor, run)
            job = self._insert_job_in_transaction(cursor, run.id, stage)
        run.version = next_version
        run.updated_at = updated_at
        return run, job

    def save_run_with_lease(
        self, run: RunRecord, job_id: str, worker_id: str, fencing_token: int
    ) -> RunRecord:
        from psycopg.types.json import Jsonb

        expected = run.version
        next_version = expected + 1
        candidate = run.model_copy(update={"version": next_version, "updated_at": utc_now()})
        now = utc_now()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM agent_jobs
                WHERE id=%s AND run_id=%s AND status=%s AND owner=%s
                    AND fencing_token=%s AND lease_until>%s
                FOR UPDATE
                """,
                (job_id, run.id, JobStatus.CLAIMED, worker_id, fencing_token, now),
            )
            if cursor.fetchone() is None:
                raise LeaseLostError(f"job {job_id} fencing token is stale")
            cursor.execute("SELECT version FROM agent_runs WHERE id=%s FOR UPDATE", (run.id,))
            row = cursor.fetchone()
            if row is None or int(row[0]) != expected:
                current = "missing" if row is None else int(row[0])
                raise ConcurrencyError(
                    f"运行 {run.id} 版本冲突：期望 {expected}，当前 {current}"
                )
            cursor.execute(
                "UPDATE agent_runs SET payload=%s,updated_at=%s,version=%s WHERE id=%s AND version=%s",
                (
                    Jsonb(candidate.model_dump(mode="json")),
                    candidate.updated_at,
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
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT payload,version FROM agent_runs WHERE id=%s", (run_id,))
            row = cursor.fetchone()
        return _with_version(row[0], int(row[1])) if row else None

    def list_runs(
        self, tenant_id: str | None = None, limit: int | None = None
    ) -> list[RunRecord]:
        query = "SELECT payload,version FROM agent_runs"
        parameters: list[str | int] = []
        if tenant_id is not None:
            query += " WHERE payload->>'tenant_id'=%s"
            parameters.append(tenant_id)
        query += " ORDER BY updated_at DESC"
        if limit is not None:
            query += " LIMIT %s"
            parameters.append(limit)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(query, parameters)
            rows = cursor.fetchall()
        return [_with_version(row[0], int(row[1])) for row in rows]

    def enqueue_job(self, run_id: str, stage: str) -> JobRecord:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM agent_runs WHERE id=%s FOR UPDATE", (run_id,))
            if cursor.fetchone() is None:
                raise ValueError(f"run {run_id} does not exist")
            return self._insert_job_in_transaction(cursor, run_id, stage)

    def reconcile_orphaned_runs(self) -> list[JobRecord]:
        repaired: list[JobRecord] = []
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,payload,version FROM agent_runs ORDER BY updated_at FOR UPDATE"
            )
            rows = cursor.fetchall()
            for run_id, payload, version in rows:
                run = _with_version(payload, int(version))
                if run.status != "queued":
                    continue
                cursor.execute(
                    """
                    SELECT 1 FROM agent_jobs
                    WHERE run_id=%s AND status IN (%s, %s) LIMIT 1
                    """,
                    (run_id, *ACTIVE_JOB_STATUSES),
                )
                if cursor.fetchone() is None:
                    repaired.append(
                        self._insert_job_in_transaction(
                            cursor, run_id, _recovery_stage(run)
                        )
                    )
        return repaired

    def claim_job(self, worker_id: str, lease_seconds: int) -> JobRecord | None:
        now = utc_now()
        deadline = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                WITH candidate AS (
                    SELECT id, status FROM agent_jobs
                    WHERE (status=%s AND available_at<=%s)
                       OR (status=%s AND lease_until IS NOT NULL AND lease_until<=%s)
                    ORDER BY CASE WHEN status=%s THEN 0 ELSE 1 END, available_at, created_at
                    FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE agent_jobs j
                SET status=%s, owner=%s, lease_until=%s, fencing_token=j.fencing_token+1,
                    attempts=j.attempts+1, updated_at=%s,
                    last_error=CASE WHEN candidate.status=%s THEN 'lease expired; recovered' ELSE j.last_error END
                FROM candidate WHERE j.id=candidate.id
                RETURNING {POSTGRES_JOB_COLUMNS_FROM_UPDATE}
                """,
                (
                    JobStatus.QUEUED,
                    now,
                    JobStatus.CLAIMED,
                    now,
                    JobStatus.CLAIMED,
                    JobStatus.CLAIMED,
                    worker_id,
                    deadline,
                    now,
                    JobStatus.CLAIMED,
                ),
            )
            row = cursor.fetchone()
        return _job_from_row(row) if row else None

    def heartbeat_job(
        self, job_id: str, worker_id: str, fencing_token: int, lease_seconds: int
    ) -> JobRecord:
        now = utc_now()
        deadline = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE agent_jobs SET lease_until=%s,updated_at=%s
                WHERE id=%s AND status=%s AND owner=%s AND fencing_token=%s AND lease_until>%s
                RETURNING {JOB_COLUMNS}
                """,
                (deadline, now, job_id, JobStatus.CLAIMED, worker_id, fencing_token, now),
            )
            row = cursor.fetchone()
        if row is None:
            raise LeaseLostError(f"job {job_id} lease has been lost")
        return _job_from_row(row)

    def assert_job_lease(
        self, job_id: str, worker_id: str, fencing_token: int
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT 1 FROM agent_jobs
                WHERE id=%s AND status=%s AND owner=%s AND fencing_token=%s AND lease_until>%s
                """,
                (job_id, JobStatus.CLAIMED, worker_id, fencing_token, utc_now()),
            )
            row = cursor.fetchone()
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
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE agent_jobs SET status=%s,lease_until=NULL,updated_at=%s,last_error=%s
                WHERE id=%s AND status=%s AND owner=%s AND fencing_token=%s
                RETURNING {JOB_COLUMNS}
                """,
                (status, now, error, job_id, JobStatus.CLAIMED, worker_id, fencing_token),
            )
            row = cursor.fetchone()
        if row is None:
            raise LeaseLostError(f"job {job_id} cannot be finished by stale worker")
        return _job_from_row(row)

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
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE agent_jobs SET status=%s,owner=NULL,lease_until=NULL,available_at=%s,
                    updated_at=%s,last_error=%s
                WHERE id=%s AND status=%s AND owner=%s AND fencing_token=%s
                RETURNING {JOB_COLUMNS}
                """,
                (
                    JobStatus.QUEUED,
                    available,
                    now,
                    error,
                    job_id,
                    JobStatus.CLAIMED,
                    worker_id,
                    fencing_token,
                ),
            )
            row = cursor.fetchone()
        if row is None:
            raise LeaseLostError(f"job {job_id} cannot be requeued by stale worker")
        return _job_from_row(row)

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT {JOB_COLUMNS} FROM agent_jobs WHERE id=%s", (job_id,))
            row = cursor.fetchone()
        return _job_from_row(row) if row else None

    def list_jobs(self, run_id: str | None = None) -> list[JobRecord]:
        with self._connect() as connection, connection.cursor() as cursor:
            if run_id:
                cursor.execute(
                    f"SELECT {JOB_COLUMNS} FROM agent_jobs WHERE run_id=%s ORDER BY created_at DESC",
                    (run_id,),
                )
            else:
                cursor.execute(f"SELECT {JOB_COLUMNS} FROM agent_jobs ORDER BY created_at DESC")
            rows = cursor.fetchall()
        return [_job_from_row(row) for row in rows]

    def save_evaluation(self, report: EvaluationReport) -> EvaluationReport:
        from psycopg.types.json import Jsonb

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO evaluation_reports(id,tenant_id,payload,created_at)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET tenant_id=excluded.tenant_id,
                    payload=excluded.payload,created_at=excluded.created_at
                """,
                (
                    report.id,
                    report.tenant_id,
                    Jsonb(report.model_dump(mode="json")),
                    report.created_at,
                ),
            )
        return report

    def latest_evaluation(self, tenant_id: str) -> EvaluationReport | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload FROM evaluation_reports WHERE tenant_id=%s "
                "ORDER BY created_at DESC LIMIT 1",
                (tenant_id,),
            )
            row = cursor.fetchone()
        return EvaluationReport.model_validate(row[0]) if row else None
