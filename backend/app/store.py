from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from .migrations import (
    postgres_schema_status,
    run_postgres_migrations,
    run_sqlite_migrations,
    sqlite_schema_status,
)
from .schemas import EvaluationReport, JobRecord, JobStatus, RunRecord, utc_now


class ConcurrencyError(RuntimeError):
    pass


class LeaseLostError(ConcurrencyError):
    pass


class Store(ABC):
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


def _with_version(payload: str | dict, version: int) -> RunRecord:
    run = (
        RunRecord.model_validate_json(payload)
        if isinstance(payload, str)
        else RunRecord.model_validate(payload)
    )
    run.version = version
    return run


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    result = datetime.fromisoformat(value)
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def _job_from_row(row) -> JobRecord:
    return JobRecord(
        id=row["id"] if isinstance(row, sqlite3.Row) else row[0],
        run_id=row["run_id"] if isinstance(row, sqlite3.Row) else row[1],
        stage=row["stage"] if isinstance(row, sqlite3.Row) else row[2],
        status=row["status"] if isinstance(row, sqlite3.Row) else row[3],
        owner=row["owner"] if isinstance(row, sqlite3.Row) else row[4],
        lease_until=_parse_datetime(row["lease_until"] if isinstance(row, sqlite3.Row) else row[5]),
        fencing_token=int(row["fencing_token"] if isinstance(row, sqlite3.Row) else row[6]),
        attempts=int(row["attempts"] if isinstance(row, sqlite3.Row) else row[7]),
        available_at=_parse_datetime(row["available_at"] if isinstance(row, sqlite3.Row) else row[8]),
        created_at=_parse_datetime(row["created_at"] if isinstance(row, sqlite3.Row) else row[9]),
        updated_at=_parse_datetime(row["updated_at"] if isinstance(row, sqlite3.Row) else row[10]),
        last_error=row["last_error"] if isinstance(row, sqlite3.Row) else row[11],
    )


JOB_COLUMNS = (
    "id, run_id, stage, status, owner, lease_until, fencing_token, attempts, "
    "available_at, created_at, updated_at, last_error"
)
POSTGRES_JOB_COLUMNS_FROM_UPDATE = ", ".join(
    f"j.{column.strip()}" for column in JOB_COLUMNS.split(",")
)

ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.CLAIMED)


def _new_job(run_id: str, stage: str, *, now: datetime | None = None) -> JobRecord:
    created_at = now or utc_now()
    return JobRecord(
        id=f"JOB-{uuid4().hex[:12].upper()}",
        run_id=run_id,
        stage=stage,
        available_at=created_at,
        created_at=created_at,
        updated_at=created_at,
    )


def _recovery_stage(run: RunRecord) -> str:
    if run.current_node == "queued":
        return "start"
    if run.current_node == "execute" and run.approval.decision == "approved":
        return "resume"
    return "retry"


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


class PostgresStore(Store):
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def _connect(self):
        import psycopg

        return psycopg.connect(self.database_url)

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


def create_store(database_url: str) -> Store:
    if database_url.startswith(("postgres://", "postgresql://")):
        return PostgresStore(database_url)
    if not database_url.startswith("sqlite:///"):
        raise ValueError("DATABASE_URL 仅支持 sqlite:/// 或 postgresql://")
    return SQLiteStore(database_url)
