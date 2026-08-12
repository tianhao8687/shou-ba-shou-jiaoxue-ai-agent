from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "failed", "handed_off", "cancelled"}


class ReliabilityFailure(RuntimeError):
    pass


def http_json(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    expected: int = 200,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{base_url.rstrip('/')}{path}", body, headers=headers, method=method
    )
    try:
        with urlopen(request, timeout=20) as response:
            status = response.status
            raw = response.read()
    except HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except URLError as exc:
        raise ReliabilityFailure(f"{method} {path} failed: {exc.reason}") from exc
    if status != expected:
        raise ReliabilityFailure(
            f"{method} {path} returned {status}, expected {expected}: "
            f"{raw.decode('utf-8', errors='replace')[:1000]}"
        )
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ReliabilityFailure(f"{method} {path} did not return an object")
    return value


def compose(args: argparse.Namespace, *arguments: str) -> str:
    command = compose_command(args, *arguments)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise ReliabilityFailure(
            f"{' '.join(command)} failed: {completed.stderr.strip()[-1500:]}"
        )
    return completed.stdout.strip()


def compose_command(args: argparse.Namespace, *arguments: str) -> list[str]:
    command = ["docker", "compose"]
    for path in args.compose_file:
        command.extend(["-f", str(path)])
    command.extend(arguments)
    return command


def login(args: argparse.Namespace) -> str:
    response = http_json(
        args.base_url,
        "POST",
        "/api/auth/login",
        payload={"username": "admin@harbor.local", "password": args.password},
    )
    token = str(response.get("access_token", ""))
    if not token:
        raise ReliabilityFailure("login did not return an access token")
    return token


def wait_ready(
    args: argparse.Namespace, *, ready: bool, timeout: float = 120
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            expected = 200 if ready else 503
            last = http_json(args.base_url, "GET", "/api/ready", expected=expected)
            if bool(last.get("ready")) is ready:
                return last
        except (ReliabilityFailure, json.JSONDecodeError):
            if not ready:
                return {"ready": False, "transport": "unavailable"}
        time.sleep(0.5)
    raise ReliabilityFailure(
        f"readiness did not become {ready} in {timeout:.0f}s: {last}"
    )


def poll_run(
    args: argparse.Namespace, token: str, run_id: str, timeout: float = 120
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = http_json(
                args.base_url, "GET", f"/api/runs/{run_id}", token=token
            )
        except ReliabilityFailure:
            time.sleep(0.5)
            continue
        status = str(last.get("status", "unknown"))
        if status in TERMINAL:
            return last
        time.sleep(0.25)
    raise ReliabilityFailure(
        f"run {run_id} did not finish; last status={last.get('status', 'unknown')}"
    )


def start_run(args: argparse.Namespace, token: str) -> str:
    drill = http_json(
        args.base_url,
        "POST",
        "/api/drills",
        token=token,
        payload={"fault_kind": "stale_cache"},
        expected=201,
    )
    created = http_json(
        args.base_url,
        "POST",
        "/api/runs",
        token=token,
        payload={"incident": drill["incident"]},
        expected=202,
    )
    run_id = str(created.get("id", ""))
    if not run_id.startswith("RUN-"):
        raise ReliabilityFailure("run creation did not return an id")
    return run_id


def assert_completed(run: dict[str, Any]) -> dict[str, Any]:
    if run.get("status") != "completed":
        raise ReliabilityFailure(
            f"run {run.get('id')} ended as {run.get('status')}: "
            f"{run.get('error_code')} {run.get('error_message')}"
        )
    if not run.get("verification") or not all(
        item.get("passed") is True for item in run["verification"]
    ):
        raise ReliabilityFailure(f"run {run.get('id')} lacks passing verification")
    return {
        "run_id": run["id"],
        "status": run["status"],
        "version": run["version"],
        "recovered": any(
            event.get("action") == "job.recovered" for event in run.get("audit", [])
        ),
    }


def restart_one_worker(args: argparse.Namespace, index: int) -> dict[str, Any]:
    workers = [line for line in compose(args, "ps", "-q", "worker").splitlines() if line]
    if not workers:
        raise ReliabilityFailure("no Compose worker containers were found")
    target = workers[index % len(workers)]
    started = time.monotonic()
    completed = subprocess.run(
        ["docker", "restart", target],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise ReliabilityFailure(f"docker restart failed: {completed.stderr.strip()}")
    return {
        "kind": "worker_restart",
        "container_id": target[:12],
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def wait_worker_count(
    args: argparse.Namespace, expected: int | None, timeout: float = 60
) -> dict[str, Any]:
    if expected is None:
        return wait_ready(args, ready=True)
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = wait_ready(args, ready=True)
        observed = last.get("worker_runtime", {}).get("registered_worker_count")
        if observed == expected:
            return last
        time.sleep(0.5)
    raise ReliabilityFailure(
        f"worker registry did not recover to {expected}; "
        f"observed {last.get('worker_runtime', {}).get('registered_worker_count')}"
    )


def restart_database(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    compose(args, "restart", "database")
    recovered = wait_ready(args, ready=True)
    return {
        "kind": "database_restart",
        "duration_seconds": round(time.monotonic() - started, 3),
        "schema": recovered.get("database_status", {}).get("schema"),
    }


def poll_claimed_job(
    args: argparse.Namespace, token: str, run_id: str, timeout: float = 30
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        request = Request(
            f"{args.base_url.rstrip('/')}/api/runs/{run_id}/jobs",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, list):
                last = payload
                claimed = next(
                    (item for item in payload if item.get("status") == "claimed"),
                    None,
                )
                if claimed is not None:
                    return claimed
        except (URLError, HTTPError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise ReliabilityFailure(
        f"run {run_id} was not observed in claimed state before timeout: {last}"
    )


def wait_agent_runs_lock(args: argparse.Namespace, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    query = (
        "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid=l.relation "
        "WHERE c.relname='agent_runs' AND l.mode='AccessExclusiveLock' AND l.granted"
    )
    while time.monotonic() < deadline:
        observed = compose(
            args,
            "exec",
            "-T",
            "database",
            "psql",
            "-U",
            "harbor",
            "-d",
            "harbor",
            "-Atc",
            query,
        )
        if observed.strip() == "1":
            return
        time.sleep(0.1)
    raise ReliabilityFailure("failed to acquire deterministic agent_runs crash-window lock")


def restart_database_after_claim(
    args: argparse.Namespace, token: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Force the exact claim -> first Run read PostgreSQL crash window."""
    compose(args, "stop", "worker")
    run_id = start_run(args, token)
    lock_command = compose_command(
        args,
        "exec",
        "-T",
        "database",
        "psql",
        "-U",
        "harbor",
        "-d",
        "harbor",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "BEGIN; LOCK TABLE agent_runs IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep(120);",
    )
    lock_process = subprocess.Popen(
        lock_command,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_agent_runs_lock(args)
        compose(args, "start", "worker")
        claimed = poll_claimed_job(args, token, run_id)
        started = time.monotonic()
        compose(args, "restart", "database")
        wait_ready(args, ready=True)
        compose(args, "start", "worker")
        run = poll_run(args, token, run_id, timeout=150)
        result = assert_completed(run)
        result["latency_seconds"] = round(time.monotonic() - started, 3)
        evidence = {
            "kind": "database_restart_after_job_claim_before_node",
            "run_id": run_id,
            "job_id": claimed.get("id"),
            "fencing_token_before_restart": claimed.get("fencing_token"),
            "run_preserved": True,
            "terminal_status": run.get("status"),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        return evidence, result
    finally:
        if lock_process.poll() is None:
            lock_process.terminate()
            try:
                lock_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                lock_process.kill()
        compose(args, "start", "worker")


def prometheus_outage(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    compose(args, "stop", "prometheus")
    degraded = wait_ready(args, ready=False)
    compose(args, "start", "prometheus")
    recovered = wait_ready(args, ready=True)
    return {
        "kind": "prometheus_outage",
        "duration_seconds": round(time.monotonic() - started, 3),
        "degraded_tool_status": degraded.get("tool_runtime", {}).get("status"),
        "recovered_tool_status": recovered.get("tool_runtime", {}).get("status"),
    }


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def execute(args: argparse.Namespace) -> dict[str, Any]:
    started_wall = datetime.now(timezone.utc)
    started = time.monotonic()
    initial = wait_ready(args, ready=True)
    token = login(args)
    results: list[dict[str, Any]] = []
    injections: list[dict[str, Any]] = []
    if args.database_restart_after_claim:
        injection, recovered = restart_database_after_claim(args, token)
        injections.append(injection)
        results.append(recovered)
        token = login(args)
    prometheus_injected = False
    iteration = 0
    deadline = started + max(0.0, args.duration_seconds)
    while iteration < args.min_runs or time.monotonic() < deadline:
        iteration += 1
        run_started = time.monotonic()
        run_id = start_run(args, token)
        if args.worker_restart_every and iteration % args.worker_restart_every == 0:
            restart_index = iteration // args.worker_restart_every - 1
            injections.append(restart_one_worker(args, restart_index))
        run = poll_run(args, token, run_id)
        result = assert_completed(run)
        result["latency_seconds"] = round(time.monotonic() - run_started, 3)
        results.append(result)

        if args.database_restart_every and iteration % args.database_restart_every == 0:
            injections.append(restart_database(args))
            token = login(args)
        if args.prometheus_outage_once and not prometheus_injected:
            injections.append(prometheus_outage(args))
            prometheus_injected = True

    initial_worker_count = initial.get("worker_runtime", {}).get(
        "registered_worker_count"
    )
    final = wait_worker_count(args, initial_worker_count)
    completed_at = datetime.now(timezone.utc)
    report = {
        "format": "harbor-reliability-evidence/v1",
        "status": "passed",
        "started_at": started_wall.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "runs_requested_minimum": args.min_runs,
        "runs_completed": len(results),
        "failed_runs": 0,
        "recovered_runs": sum(1 for item in results if item["recovered"]),
        "latency_seconds": {
            "max": max(item["latency_seconds"] for item in results),
            "mean": round(
                sum(item["latency_seconds"] for item in results) / len(results), 3
            ),
        },
        "fault_injections": injections,
        "initial_worker_count": initial_worker_count,
        "final_worker_count": final.get("worker_runtime", {}).get(
            "registered_worker_count"
        ),
        "schema": final.get("database_status", {}).get("schema"),
        "runs": results,
    }
    atomic_json(args.output.resolve(), report)
    return {**report, "evidence_path": str(args.output.resolve())}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run repeatable AgentOps soak traffic with optional dependency faults."
    )
    result.add_argument("--base-url", default="http://127.0.0.1:8000")
    result.add_argument("--password", default="harbor-demo-2026")
    result.add_argument("--duration-seconds", type=float, default=300)
    result.add_argument("--min-runs", type=int, default=10)
    result.add_argument("--worker-restart-every", type=int, default=0)
    result.add_argument("--database-restart-every", type=int, default=0)
    result.add_argument("--database-restart-after-claim", action="store_true")
    result.add_argument("--prometheus-outage-once", action="store_true")
    result.add_argument(
        "--compose-file", action="append", type=Path, default=None
    )
    result.add_argument(
        "--output",
        type=Path,
        default=ROOT / "artifacts" / "reliability" / "latest.json",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    args.compose_file = args.compose_file or [ROOT / "docker-compose.yml"]
    if args.min_runs < 1 or args.duration_seconds < 0:
        print("reliability lab requires min-runs >= 1 and duration >= 0", file=sys.stderr)
        return 2
    try:
        report = execute(args)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (ReliabilityFailure, OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"reliability lab failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
