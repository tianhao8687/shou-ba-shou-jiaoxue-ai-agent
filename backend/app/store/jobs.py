from __future__ import annotations

from datetime import datetime
import sqlite3
from uuid import uuid4

from ..schemas import JobRecord, JobStatus, RunRecord, utc_now
from .runs import _parse_datetime

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
