from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TERMINAL_RUN_STATUSES = {"completed", "failed", "handed_off", "cancelled"}


class SmokeFailure(RuntimeError):
    pass


@dataclass
class HttpResult:
    status: int
    headers: Any
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SmokeFailure("response is not valid UTF-8 JSON") from exc


class HarborClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
        expected_status: int = 200,
    ) -> HttpResult:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = HttpResult(response.status, response.headers, response.read())
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SmokeFailure(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise SmokeFailure(f"{method} {path} could not connect: {exc.reason}") from exc
        if result.status != expected_status:
            raise SmokeFailure(
                f"{method} {path} returned HTTP {result.status}; expected {expected_status}"
            )
        return result


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def poll_run(
    client: HarborClient,
    run_id: str,
    token: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_status = "unknown"
    while time.monotonic() < deadline:
        run = client.request("GET", f"/api/runs/{run_id}", token=token).json()
        last_status = str(run.get("status", "unknown"))
        if last_status in TERMINAL_RUN_STATUSES:
            return run
        if last_status == "awaiting_approval":
            raise SmokeFailure("stale-cache smoke run unexpectedly requires human approval")
        time.sleep(0.5)
    raise SmokeFailure(f"run {run_id} did not finish in {timeout:.0f}s; last status={last_status}")


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    client = HarborClient(args.base_url, min(args.timeout, 30.0))
    checks: list[str] = []

    index = client.request("GET", "/")
    html = index.body.decode("utf-8", errors="replace")
    require('<div id="root">' in html, "frontend root element was not served")
    require(
        "default-src 'self'" in (index.headers.get("Content-Security-Policy") or ""),
        "frontend response is missing the expected Content-Security-Policy",
    )
    require(
        (index.headers.get("X-Content-Type-Options") or "").lower() == "nosniff",
        "frontend response is missing X-Content-Type-Options: nosniff",
    )
    checks.append("frontend-and-security-headers")

    health = client.request("GET", "/api/health").json()
    require(health.get("status") == "ok", "API health status is not ok")
    if args.expect_database:
        require(
            health.get("database") == args.expect_database,
            f"database is {health.get('database')!r}, expected {args.expect_database!r}",
        )
    model_status = str(health.get("model_runtime", {}).get("status", "unknown"))
    if args.expect_model_status:
        require(
            model_status == args.expect_model_status,
            f"model status is {model_status!r}, expected {args.expect_model_status!r}",
        )
    checks.append("truthful-runtime-health")

    auth = client.request(
        "POST",
        "/api/auth/login",
        payload={"username": args.username, "password": args.password},
    ).json()
    token = str(auth.get("access_token", ""))
    require(bool(token), "login response did not contain an access token")
    identity = client.request("GET", "/api/auth/me", token=token).json()
    require(identity.get("username") == args.username, "authenticated identity does not match")
    require("admin" in identity.get("roles", []), "smoke account does not have admin role")
    checks.append("authentication-and-rbac")

    drill = client.request(
        "POST",
        "/api/drills",
        token=token,
        payload={"fault_kind": "stale_cache"},
        expected_status=201,
    ).json()
    incident = drill.get("incident")
    require(isinstance(incident, dict), "drill response did not contain an incident")
    started_run = client.request(
        "POST",
        "/api/runs",
        token=token,
        payload={"incident": incident},
        expected_status=202,
    ).json()
    run_id = str(started_run.get("id", ""))
    require(run_id.startswith("RUN-"), "run response did not contain a valid run id")
    run = poll_run(client, run_id, token, args.timeout)

    require(run.get("status") == "completed", f"run ended as {run.get('status')!r}")
    require(
        run.get("approval", {}).get("decision") == "not_required",
        "low-risk cache remediation should not require approval",
    )
    require(bool(run.get("sources")), "run did not retain retrieval sources")
    require(bool(run.get("observations")), "run did not retain tool observations")
    require(bool(run.get("plan")), "run did not materialize a remediation plan")
    tool_names = [item.get("tool_name") for item in run.get("tool_results", [])]
    require("refresh_cache" in tool_names, "run did not execute the scoped cache refresh")
    verification = run.get("verification", [])
    require(bool(verification), "run did not record independent verification")
    require(all(item.get("passed") is True for item in verification), "verification failed")
    require(bool(run.get("resolution")), "run did not record a resolution")
    checks.append("complete-agent-remediation-loop")

    jobs = client.request("GET", f"/api/runs/{run_id}/jobs", token=token).json()
    require(bool(jobs), "run has no durable job record")
    require(any(job.get("status") == "succeeded" for job in jobs), "job did not succeed")
    require(any(int(job.get("fencing_token", 0)) >= 1 for job in jobs), "job has no fencing token")
    checks.append("durable-job-and-fencing")

    evidence = client.request("GET", f"/api/runs/{run_id}/evidence", token=token).json()
    require(
        evidence.get("runtime_leak_check", {}).get("passed") is True,
        "evidence bundle leak check failed",
    )
    require(
        evidence.get("contract", {}).get("model_mode") == run.get("run_mode"),
        "evidence contract does not match the run model mode",
    )
    checks.append("evidence-bundle-and-leak-check")

    return {
        "status": "passed",
        "duration_seconds": round(time.monotonic() - started, 3),
        "base_url": args.base_url,
        "run_id": run_id,
        "run_mode": run.get("run_mode"),
        "model_status": model_status,
        "database": health.get("database"),
        "vector_quality": health.get("vector_quality"),
        "tool_transport": evidence.get("contract", {}).get("tool_transport"),
        "checks": checks,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a real HTTP smoke test through Nginx, API, worker and fault lab."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--username", default="admin@harbor.local")
    parser.add_argument(
        "--password",
        default=os.getenv("DEMO_PASSWORD", "harbor-demo-2026"),
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--expect-database", default="")
    parser.add_argument("--expect-model-status", default="")
    args = parser.parse_args()
    if args.timeout < 5 or args.timeout > 600:
        parser.error("--timeout must be between 5 and 600 seconds")
    if not args.base_url.startswith(("http://", "https://")):
        parser.error("--base-url must start with http:// or https://")
    return args


def main() -> int:
    args = parse_args()
    try:
        result = run_smoke(args)
    except SmokeFailure as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
