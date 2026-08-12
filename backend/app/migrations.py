from __future__ import annotations

from dataclasses import dataclass
import hashlib
import sqlite3
from typing import Any


class MigrationError(RuntimeError):
    """Raised when a schema cannot be migrated without losing integrity."""


class MigrationDriftError(MigrationError):
    """An already-applied migration no longer matches the source definition."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sqlite_sql: str
    postgres_sql: str

    def checksum(self, engine: str) -> str:
        sql = self.sqlite_sql if engine == "sqlite" else self.postgres_sql
        canonical = f"{self.version}\n{self.name}\n{sql.strip()}\n"
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


MIGRATIONS = (
    Migration(
        1,
        "durable_run_job_and_evaluation_baseline",
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_agent_runs_updated
            ON agent_runs(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant_updated
            ON agent_runs(json_extract(payload, '$.tenant_id'), updated_at DESC);
        CREATE TABLE IF NOT EXISTS agent_jobs (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            owner TEXT,
            lease_until TEXT,
            fencing_token INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_error TEXT,
            FOREIGN KEY(run_id) REFERENCES agent_runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_agent_jobs_claim
            ON agent_jobs(status, available_at, lease_until, created_at);
        CREATE INDEX IF NOT EXISTS idx_agent_jobs_run
            ON agent_jobs(run_id, created_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_jobs_active_stage
            ON agent_jobs(run_id, stage)
            WHERE status IN ('queued', 'claimed');
        CREATE TABLE IF NOT EXISTS evaluation_reports (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'xm-ops',
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY,
            payload JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            version INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_agent_runs_updated
            ON agent_runs(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_runs_tenant_updated
            ON agent_runs((payload->>'tenant_id'), updated_at DESC);
        CREATE TABLE IF NOT EXISTS agent_jobs (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES agent_runs(id),
            stage TEXT NOT NULL,
            status TEXT NOT NULL,
            owner TEXT,
            lease_until TIMESTAMPTZ,
            fencing_token INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_agent_jobs_claim
            ON agent_jobs(status, available_at, lease_until, created_at);
        CREATE INDEX IF NOT EXISTS idx_agent_jobs_run
            ON agent_jobs(run_id, created_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_jobs_active_stage
            ON agent_jobs(run_id, stage)
            WHERE status IN ('queued', 'claimed');
        CREATE TABLE IF NOT EXISTS evaluation_reports (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'xm-ops',
            payload JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        );
        """,
    ),
    Migration(
        2,
        "tenant_scoped_evaluation_reports",
        """
        CREATE INDEX IF NOT EXISTS idx_evaluation_reports_tenant_created
            ON evaluation_reports(tenant_id, created_at DESC);
        """,
        """
        ALTER TABLE evaluation_reports
            ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'xm-ops';
        CREATE INDEX IF NOT EXISTS idx_evaluation_reports_tenant_created
            ON evaluation_reports(tenant_id, created_at DESC);
        """,
    ),
    Migration(
        3,
        "worker_process_heartbeats",
        """
        CREATE TABLE IF NOT EXISTS worker_instances (
            worker_id TEXT PRIMARY KEY,
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_worker_instances_last_seen
            ON worker_instances(last_seen_at DESC);
        """,
        """
        CREATE TABLE IF NOT EXISTS worker_instances (
            worker_id TEXT PRIMARY KEY,
            started_at TIMESTAMPTZ NOT NULL,
            last_seen_at TIMESTAMPTZ NOT NULL,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX IF NOT EXISTS idx_worker_instances_last_seen
            ON worker_instances(last_seen_at DESC);
        """,
    ),
)

LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


def _sqlite_statements(sql: str) -> list[str]:
    return [statement.strip() for statement in sql.split(";") if statement.strip()]


def _ensure_sqlite_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def run_sqlite_migrations(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.execute("BEGIN IMMEDIATE")
    _ensure_sqlite_migration_table(connection)
    rows = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    applied = {int(row[0]): (str(row[1]), str(row[2])) for row in rows}
    if applied and max(applied) > LATEST_SCHEMA_VERSION:
        raise MigrationError(
            f"database schema version {max(applied)} is newer than supported "
            f"version {LATEST_SCHEMA_VERSION}"
        )
    newly_applied: list[int] = []
    for migration in MIGRATIONS:
        checksum = migration.checksum("sqlite")
        recorded = applied.get(migration.version)
        if recorded is not None:
            if recorded != (migration.name, checksum):
                raise MigrationDriftError(
                    f"migration {migration.version} ({migration.name}) checksum drift"
                )
            continue
        if migration.version == 2:
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(evaluation_reports)")
            }
            if "tenant_id" not in columns:
                connection.execute(
                    "ALTER TABLE evaluation_reports ADD COLUMN tenant_id TEXT "
                    "NOT NULL DEFAULT 'xm-ops'"
                )
        for statement in _sqlite_statements(migration.sqlite_sql):
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, name, checksum) VALUES (?, ?, ?)",
            (migration.version, migration.name, checksum),
        )
        newly_applied.append(migration.version)
    return {
        "status": "current",
        "current_version": LATEST_SCHEMA_VERSION,
        "latest_version": LATEST_SCHEMA_VERSION,
        "newly_applied": newly_applied,
    }


def run_postgres_migrations(connection: Any) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(" 
            "current_database() || ':harbor-agentops:schema-migrations', 0))"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        )
        applied = {int(row[0]): (str(row[1]), str(row[2])) for row in cursor.fetchall()}
        if applied and max(applied) > LATEST_SCHEMA_VERSION:
            raise MigrationError(
                f"database schema version {max(applied)} is newer than supported "
                f"version {LATEST_SCHEMA_VERSION}"
            )
        newly_applied: list[int] = []
        for migration in MIGRATIONS:
            checksum = migration.checksum("postgresql")
            recorded = applied.get(migration.version)
            if recorded is not None:
                if recorded != (migration.name, checksum):
                    raise MigrationDriftError(
                        f"migration {migration.version} ({migration.name}) checksum drift"
                    )
                continue
            cursor.execute(migration.postgres_sql)
            cursor.execute(
                "INSERT INTO schema_migrations(version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, checksum),
            )
            newly_applied.append(migration.version)
    return {
        "status": "current",
        "current_version": LATEST_SCHEMA_VERSION,
        "latest_version": LATEST_SCHEMA_VERSION,
        "newly_applied": newly_applied,
    }


def sqlite_schema_status(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    current = int(row[0] or 0)
    return {
        "status": "current" if current == LATEST_SCHEMA_VERSION else "behind",
        "current_version": current,
        "latest_version": LATEST_SCHEMA_VERSION,
    }


def postgres_schema_status(connection: Any) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT MAX(version) FROM schema_migrations")
        row = cursor.fetchone()
    current = int(row[0] or 0)
    return {
        "status": "current" if current == LATEST_SCHEMA_VERSION else "behind",
        "current_version": current,
        "latest_version": LATEST_SCHEMA_VERSION,
    }
