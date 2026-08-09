from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Mapping
from urllib.error import URLError
from urllib.request import urlopen


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPOSITORY_ROOT / "scripts" / "smoke-test.py"
BUILD_SERVICES = ("ops-sandbox", "backend", "worker", "frontend")


class QuickstartFailure(RuntimeError):
    pass


def docker_executable() -> str:
    executable = shutil.which("docker")
    if not executable:
        raise QuickstartFailure("Docker CLI was not found. Install Docker Desktop or Docker Engine first.")
    probe = subprocess.run(
        [executable, "info"],
        cwd=REPOSITORY_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        detail = probe.stderr.strip().splitlines()
        suffix = detail[-1] if detail else "Docker daemon is unavailable"
        raise QuickstartFailure(f"Docker is installed but not running: {suffix}")
    compose = subprocess.run(
        [executable, "compose", "version"],
        cwd=REPOSITORY_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if compose.returncode != 0:
        raise QuickstartFailure("Docker Compose v2 is required (`docker compose`).")
    return executable


def compose_command(docker: str, *arguments: str, check: bool = True) -> int:
    command = [docker, "compose", "--ansi", "never"]
    if arguments and arguments[0] == "build":
        command.extend(["--progress", "plain"])
    command.extend(arguments)
    # Inherit stdio instead of relaying it through a Python pipe. Docker Desktop
    # uses the console handles while opening its BuildKit session on Windows;
    # piping them through another process can corrupt the session header.
    completed = subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        check=False,
    )
    return_code = completed.returncode
    if check and return_code != 0:
        raise QuickstartFailure(
            f"docker compose {' '.join(arguments)} failed with exit code {return_code}"
        )
    return return_code


def compose_backend_environment(docker: str) -> dict[str, str]:
    completed = subprocess.run(
        [docker, "compose", "config", "--format", "json"],
        cwd=REPOSITORY_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        suffix = detail[-1] if detail else "unknown Compose configuration error"
        raise QuickstartFailure(f"could not resolve Compose configuration: {suffix}")
    try:
        configuration = json.loads(completed.stdout)
        environment = configuration["services"]["backend"]["environment"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise QuickstartFailure("Compose configuration has no backend environment") from exc
    if not isinstance(environment, dict):
        raise QuickstartFailure("Compose backend environment is not a key-value mapping")
    return {str(key): str(value) for key, value in environment.items() if value is not None}


def wait_for_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not ready"
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except (OSError, URLError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise QuickstartFailure(f"stack did not become ready in {timeout:.0f}s: {last_error}")


def expected_fixture_status(environment: Mapping[str, str] | None = None) -> str:
    source = environment if environment is not None else os.environ
    fixture = source.get("MODEL_FIXTURE_MODE", "true").strip().lower()
    enabled = source.get("MODEL_ENABLED", "false").strip().lower()
    if fixture in {"1", "true", "yes", "on"} and enabled not in {"1", "true", "yes", "on"}:
        return "fixture"
    return ""


def smoke_arguments(
    args: argparse.Namespace,
    runtime_environment: Mapping[str, str] | None = None,
) -> list[str]:
    command = [
        sys.executable,
        str(SMOKE_SCRIPT),
        "--base-url",
        args.base_url,
        "--timeout",
        str(args.timeout),
        "--expect-database",
        "postgresql",
    ]
    model_status = expected_fixture_status(runtime_environment)
    if model_status:
        command.extend(["--expect-model-status", model_status])
    return command


def smoke_process_environment(
    args: argparse.Namespace,
    runtime_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    child_environment = os.environ.copy()
    source = runtime_environment if runtime_environment is not None else os.environ
    password = getattr(args, "password", "") or source.get("DEMO_PASSWORD", "")
    if password:
        # Keep credentials out of command lines and process listings.
        child_environment["DEMO_PASSWORD"] = password
    return child_environment


def start(args: argparse.Namespace) -> None:
    if args.workers < 1 or args.workers > 8:
        raise QuickstartFailure("--workers must be between 1 and 8")
    docker = docker_executable()
    runtime_environment = compose_backend_environment(docker)
    try:
        if args.build:
            # Compose v5 on Docker Desktop can corrupt its shared session key
            # when several build targets are submitted together from a Windows
            # terminal. Sequential targets are cached and behave consistently
            # on Windows, macOS and Linux.
            for service in BUILD_SERVICES:
                print(f"\nBuilding {service} ...", flush=True)
                compose_command(docker, "build", service)
        compose_command(
            docker,
            "up",
            "-d",
            "--force-recreate",
            "--remove-orphans",
            "--scale",
            f"worker={args.workers}",
        )
        wait_for_http(f"{args.base_url.rstrip('/')}/api/health", args.timeout)
        if not args.skip_smoke:
            completed = subprocess.run(
                smoke_arguments(args, runtime_environment),
                cwd=REPOSITORY_ROOT,
                env=smoke_process_environment(args, runtime_environment),
                check=False,
            )
            if completed.returncode != 0:
                raise QuickstartFailure("end-to-end smoke test failed")
    except QuickstartFailure:
        compose_command(docker, "ps", check=False)
        compose_command(
            docker,
            "logs",
            "--no-color",
            "--tail",
            "40",
            "backend",
            "worker",
            "ops-sandbox",
            "frontend",
            check=False,
        )
        raise

    print("\nHarbor AgentOps is ready.")
    print(f"Web:        {args.base_url}")
    print("OpenAPI:    http://127.0.0.1:8000/docs")
    print("Prometheus: http://127.0.0.1:9090")
    print("Login:      admin@harbor.local / value of DEMO_PASSWORD")
    print("Stop:       python scripts/quickstart.py down")


def stop(args: argparse.Namespace) -> None:
    docker = docker_executable()
    command = ["down", "--remove-orphans"]
    if args.volumes:
        command.append("--volumes")
    compose_command(docker, *command)


def status(_: argparse.Namespace) -> None:
    docker = docker_executable()
    compose_command(docker, "ps")


def smoke(args: argparse.Namespace) -> None:
    runtime_environment = None
    if not args.password:
        try:
            runtime_environment = compose_backend_environment(docker_executable())
        except QuickstartFailure:
            # A remote stack can still be checked without a local Docker daemon.
            runtime_environment = None
    completed = subprocess.run(
        smoke_arguments(args, runtime_environment),
        cwd=REPOSITORY_ROOT,
        env=smoke_process_environment(args, runtime_environment),
        check=False,
    )
    if completed.returncode != 0:
        raise QuickstartFailure("end-to-end smoke test failed")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Cross-platform launcher for the reproducible Harbor AgentOps stack."
    )
    commands = root.add_subparsers(dest="command", required=True)

    up = commands.add_parser("up", help="build, start, wait and run the smoke test")
    up.add_argument("--workers", type=int, default=3)
    up.add_argument("--timeout", type=float, default=180.0)
    up.add_argument("--base-url", default="http://127.0.0.1:8080")
    up.add_argument("--build", action=argparse.BooleanOptionalAction, default=True)
    up.add_argument("--skip-smoke", action="store_true")
    up.set_defaults(handler=start)

    down = commands.add_parser("down", help="stop the stack; preserve volumes by default")
    down.add_argument("--volumes", action="store_true", help="also remove demo data volumes")
    down.set_defaults(handler=stop)

    state = commands.add_parser("status", help="show Compose service state")
    state.set_defaults(handler=status)

    verify = commands.add_parser("smoke", help="run the HTTP smoke test against a running stack")
    verify.add_argument("--timeout", type=float, default=120.0)
    verify.add_argument("--base-url", default="http://127.0.0.1:8080")
    verify.add_argument("--password", default="", help="override the configured demo password")
    verify.set_defaults(handler=smoke)
    return root


def main() -> int:
    args = parser().parse_args()
    if getattr(args, "timeout", 120.0) < 5 or getattr(args, "timeout", 120.0) > 600:
        raise SystemExit("--timeout must be between 5 and 600 seconds")
    try:
        args.handler(args)
    except QuickstartFailure as exc:
        print(f"quickstart failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("quickstart interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
