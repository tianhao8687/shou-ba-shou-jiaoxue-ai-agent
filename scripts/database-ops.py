from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATABASE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
CORE_TABLES = (
    "schema_migrations",
    "agent_runs",
    "agent_jobs",
    "evaluation_reports",
    "worker_instances",
)


class DatabaseOpsError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    input_bytes: bytes | None = None,
    text: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[Any]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        input=input_bytes,
        stdin=subprocess.DEVNULL if input_bytes is None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = completed.stderr if text else completed.stderr.decode("utf-8", "replace")
        raise DatabaseOpsError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{stderr.strip()[-2000:]}"
        )
    return completed


def compose_prefix(args: argparse.Namespace) -> list[str]:
    command = ["docker", "compose"]
    for path in args.compose_file:
        command.extend(["-f", str(path)])
    return command


def database_container(args: argparse.Namespace) -> str:
    result = run([*compose_prefix(args), "ps", "-q", args.service])
    container = result.stdout.strip()
    if not container or "\n" in container:
        raise DatabaseOpsError(
            f"expected exactly one running Compose service named {args.service!r}"
        )
    inspection = run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container]
    )
    if inspection.stdout.strip().lower() != "true":
        raise DatabaseOpsError(f"database container {container} is not running")
    return container


def exec_database(
    container: str,
    arguments: list[str],
    *,
    input_bytes: bytes | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    command = ["docker", "exec"]
    if input_bytes is not None:
        command.append("-i")
    command.extend([container, *arguments])
    return run(command, input_bytes=input_bytes, text=text)


def validate_database_name(name: str, *, allow_primary: bool = False) -> None:
    if not DATABASE_NAME_PATTERN.fullmatch(name):
        raise DatabaseOpsError(
            "database name must start with a lowercase letter and contain only "
            "lowercase letters, digits, or underscores"
        )
    forbidden = {"postgres", "template0", "template1"}
    if not allow_primary:
        forbidden.add("harbor")
    if name in forbidden:
        raise DatabaseOpsError(f"refusing to use protected database {name!r}")


def psql_scalar(
    container: str,
    user: str,
    database: str,
    sql: str,
) -> str:
    result = exec_database(
        container,
        [
            "psql",
            "--username",
            user,
            "--dbname",
            database,
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            sql,
        ],
    )
    return result.stdout.strip()


def database_exists(container: str, user: str, database: str) -> bool:
    selected = psql_scalar(
        container,
        user,
        "postgres",
        "SELECT 1 FROM pg_database WHERE datname = "
        + "'"
        + database.replace("'", "''")
        + "'",
    )
    return selected == "1"


def snapshot_metadata(container: str, user: str, database: str) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for table in CORE_TABLES:
        counts[table] = int(psql_scalar(container, user, database, f"SELECT count(*) FROM {table}"))
    schema_version = int(
        psql_scalar(
            container,
            user,
            database,
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
        )
    )
    orphaned_jobs = int(
        psql_scalar(
            container,
            user,
            database,
            "SELECT count(*) FROM agent_jobs j LEFT JOIN agent_runs r ON r.id=j.run_id "
            "WHERE r.id IS NULL",
        )
    )
    return {
        "database": database,
        "schema_version": schema_version,
        "table_counts": counts,
        "integrity": {"orphaned_jobs": orphaned_jobs},
    }


def manifest_path(backup_path: Path) -> Path:
    return backup_path.with_suffix(backup_path.suffix + ".manifest.json")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(data)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def backup(args: argparse.Namespace) -> dict[str, Any]:
    container = database_container(args)
    validate_database_name(args.database, allow_primary=True)
    destination = args.output.resolve()
    completed = exec_database(
        container,
        [
            "pg_dump",
            "--username",
            args.user,
            "--dbname",
            args.database,
            "--format=custom",
            "--compress=6",
            "--no-owner",
            "--no-acl",
        ],
        text=False,
    )
    payload: bytes = completed.stdout
    if not payload.startswith(b"PGDMP"):
        raise DatabaseOpsError("pg_dump did not return a PostgreSQL custom archive")
    atomic_write(destination, payload)
    metadata = snapshot_metadata(container, args.user, args.database)
    manifest = {
        "format": "harbor-postgresql-backup/v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "backup_file": destination.name,
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        **metadata,
    }
    atomic_write(
        manifest_path(destination),
        (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return {"status": "backed_up", "path": str(destination), **manifest}


def load_and_validate_backup(path: Path) -> tuple[bytes, dict[str, Any] | None]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise DatabaseOpsError(f"backup does not exist: {resolved}")
    payload = resolved.read_bytes()
    if not payload.startswith(b"PGDMP"):
        raise DatabaseOpsError("backup is not a PostgreSQL custom archive")
    sidecar = manifest_path(resolved)
    manifest = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else None
    if manifest is not None:
        observed = hashlib.sha256(payload).hexdigest()
        if manifest.get("sha256") != observed:
            raise DatabaseOpsError(
                f"backup checksum mismatch: expected {manifest.get('sha256')}, got {observed}"
            )
        if int(manifest.get("size_bytes", -1)) != len(payload):
            raise DatabaseOpsError("backup size does not match its manifest")
    return payload, manifest


def create_and_restore(
    container: str,
    user: str,
    target: str,
    payload: bytes,
) -> None:
    validate_database_name(target)
    if database_exists(container, user, target):
        raise DatabaseOpsError(
            f"target database {target!r} already exists; restore only creates a new database"
        )
    exec_database(
        container,
        ["createdb", "--username", user, "--owner", user, target],
    )
    try:
        exec_database(
            container,
            [
                "pg_restore",
                "--username",
                user,
                "--dbname",
                target,
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
            ],
            input_bytes=payload,
            text=False,
        )
    except Exception:
        exec_database(
            container,
            ["dropdb", "--username", user, "--force", "--if-exists", target],
        )
        raise


def assert_restored_integrity(
    actual: dict[str, Any], manifest: dict[str, Any] | None
) -> None:
    if actual["integrity"]["orphaned_jobs"] != 0:
        raise DatabaseOpsError("restored database contains orphaned durable jobs")
    if actual["schema_version"] <= 0:
        raise DatabaseOpsError("restored database has no applied schema version")
    if manifest is None:
        return
    if actual["schema_version"] != int(manifest["schema_version"]):
        raise DatabaseOpsError("restored schema version differs from the backup manifest")
    expected_counts = manifest.get("table_counts", {})
    if actual["table_counts"] != expected_counts:
        raise DatabaseOpsError(
            "restored core table counts differ from the transaction-consistent manifest"
        )


def restore(args: argparse.Namespace) -> dict[str, Any]:
    container = database_container(args)
    payload, manifest = load_and_validate_backup(args.backup)
    create_and_restore(container, args.user, args.target_database, payload)
    actual = snapshot_metadata(container, args.user, args.target_database)
    assert_restored_integrity(actual, manifest)
    return {
        "status": "restored",
        "target_database": args.target_database,
        **actual,
    }


def verify_restore(args: argparse.Namespace) -> dict[str, Any]:
    container = database_container(args)
    payload, manifest = load_and_validate_backup(args.backup)
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    target = f"harbor_restore_verify_{suffix}"[:63]
    create_and_restore(container, args.user, target, payload)
    try:
        actual = snapshot_metadata(container, args.user, target)
        assert_restored_integrity(actual, manifest)
        return {
            "status": "restore_verified",
            "temporary_database": target,
            "temporary_database_removed": True,
            **actual,
        }
    finally:
        exec_database(
            container,
            ["dropdb", "--username", args.user, "--force", "--if-exists", target],
        )


def default_backup_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ROOT / "backups" / f"harbor-{timestamp}.dump"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Create and prove recoverable PostgreSQL backups without overwriting live data."
    )
    root.add_argument(
        "--compose-file",
        action="append",
        type=Path,
        default=None,
        help="Compose file; repeat for overrides (default: docker-compose.yml).",
    )
    root.add_argument("--service", default="database")
    root.add_argument("--user", default="harbor")
    root.add_argument("--database", default="harbor")
    commands = root.add_subparsers(dest="command", required=True)

    backup_command = commands.add_parser("backup")
    backup_command.add_argument("--output", type=Path, default=None)
    backup_command.set_defaults(handler=backup)

    verify_command = commands.add_parser("verify")
    verify_command.add_argument("backup", type=Path)
    verify_command.set_defaults(handler=verify_restore)

    restore_command = commands.add_parser("restore")
    restore_command.add_argument("backup", type=Path)
    restore_command.add_argument("--target-database", required=True)
    restore_command.set_defaults(handler=restore)
    return root


def main() -> int:
    args = parser().parse_args()
    args.compose_file = args.compose_file or [ROOT / "docker-compose.yml"]
    if args.command == "backup" and args.output is None:
        args.output = default_backup_path()
    try:
        result = args.handler(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DatabaseOpsError, OSError, json.JSONDecodeError) as exc:
        print(f"database operation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
