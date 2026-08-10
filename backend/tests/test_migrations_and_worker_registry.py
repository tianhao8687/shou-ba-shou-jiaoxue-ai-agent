from __future__ import annotations

import sqlite3
import time

import pytest

from app.migrations import LATEST_SCHEMA_VERSION, MigrationDriftError
from app.store import SQLiteStore


def test_sqlite_migrations_upgrade_legacy_schema_without_losing_rows(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE evaluation_reports (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO evaluation_reports(id, payload, created_at)
            VALUES ('EVAL-LEGACY', '{}', '2026-01-01T00:00:00+00:00');
            """
        )

    store = SQLiteStore(f"sqlite:///{path}")
    store.initialize()
    store.initialize()

    with sqlite3.connect(path) as connection:
        versions = [
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        legacy = connection.execute(
            "SELECT tenant_id FROM evaluation_reports WHERE id='EVAL-LEGACY'"
        ).fetchone()
        worker_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='worker_instances'"
        ).fetchone()

    assert versions == list(range(1, LATEST_SCHEMA_VERSION + 1))
    assert legacy == ("xm-ops",)
    assert worker_table == ("worker_instances",)
    assert store.schema_status() == {
        "status": "current",
        "current_version": LATEST_SCHEMA_VERSION,
        "latest_version": LATEST_SCHEMA_VERSION,
    }


def test_migration_checksum_drift_fails_closed(tmp_path) -> None:
    path = tmp_path / "drift.db"
    store = SQLiteStore(f"sqlite:///{path}")
    store.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum='tampered' WHERE version=1"
        )

    with pytest.raises(MigrationDriftError, match="checksum drift"):
        store.initialize()


def test_worker_registry_reports_only_live_processes_and_removes_gracefully(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'workers.db'}")
    store.initialize()
    store.heartbeat_worker("worker-a", {"hostname": "node-a", "pid": 42})

    live = store.list_live_workers(5)
    assert len(live) == 1
    assert live[0]["worker_id"] == "worker-a"
    assert live[0]["metadata"] == {"hostname": "node-a", "pid": 42}

    time.sleep(0.02)
    assert store.list_live_workers(0.001) == []
    store.heartbeat_worker("worker-a", {"hostname": "node-a", "pid": 43})
    assert len(store.list_live_workers(5)) == 1
    store.remove_worker("worker-a")
    assert store.list_live_workers(5) == []
