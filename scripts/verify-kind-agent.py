from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


NAMESPACE = "harbor-sandbox"
DEPLOYMENT = "demo-api"
TERMINAL_STATUSES = {"completed", "failed", "handed_off", "cancelled"}


class VerificationFailure(RuntimeError):
    pass


def request_json_value(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    expected_status: int = 200,
) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=30) as response:
            status = response.status
            body = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise VerificationFailure(
            f"{method} {path} returned HTTP {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise VerificationFailure(f"{method} {path} failed: {exc.reason}") from exc
    if status != expected_status:
        raise VerificationFailure(
            f"{method} {path} returned HTTP {status}; expected {expected_status}"
        )
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationFailure(f"{method} {path} did not return JSON") from exc
    return value


def request_json(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    value = request_json_value(
        base_url,
        method,
        path,
        token=token,
        payload=payload,
        expected_status=expected_status,
    )
    if not isinstance(value, dict):
        raise VerificationFailure(f"{method} {path} did not return a JSON object")
    return value


def request_json_list(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
) -> list[dict[str, Any]]:
    value = request_json_value(base_url, method, path, token=token)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise VerificationFailure(
            f"{method} {path} did not return an array of JSON objects"
        )
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationFailure(message)


def kubectl(*arguments: str) -> str:
    completed = subprocess.run(
        ["kubectl", *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise VerificationFailure(
            f"kubectl {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def scale_lab(replicas: int) -> None:
    kubectl(
        "scale",
        f"deployment/{DEPLOYMENT}",
        f"--replicas={replicas}",
        "--namespace",
        NAMESPACE,
    )
    kubectl(
        "rollout",
        "status",
        f"deployment/{DEPLOYMENT}",
        "--namespace",
        NAMESPACE,
        "--timeout=90s",
    )


def deployment_state() -> tuple[int, int]:
    raw = kubectl(
        "get",
        f"deployment/{DEPLOYMENT}",
        "--namespace",
        NAMESPACE,
        "-o",
        "json",
    )
    deployment = json.loads(raw)
    return (
        int(deployment["spec"].get("replicas", 0)),
        int(deployment.get("status", {}).get("readyReplicas", 0)),
    )


def login(base_url: str, username: str, password: str) -> str:
    response = request_json(
        base_url,
        "POST",
        "/api/auth/login",
        payload={"username": username, "password": password},
    )
    token = str(response.get("access_token", ""))
    require(bool(token), f"login for {username} did not return an access token")
    return token


def poll_for(
    base_url: str,
    run_id: str,
    token: str,
    accepted: set[str],
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = request_json(base_url, "GET", f"/api/runs/{run_id}", token=token)
        status = str(last.get("status", "unknown"))
        if status in accepted:
            return last
        if status in TERMINAL_STATUSES:
            raise VerificationFailure(
                f"run {run_id} ended as {status} before reaching {sorted(accepted)}: "
                f"{last.get('error_code')} {last.get('error_message')}"
            )
        time.sleep(0.5)
    raise VerificationFailure(
        f"run {run_id} did not reach {sorted(accepted)} in {timeout:.0f}s; "
        f"last status={last.get('status', 'unknown')}"
    )


def wait_for_initial_job_settlement(
    base_url: str,
    run_id: str,
    token: str,
    timeout: float,
) -> dict[str, Any]:
    """Wait until the worker has committed its final checkpoint for the start job.

    ``awaiting_approval`` can become visible one checkpoint before the worker marks
    its job successful. Approving that transient revision correctly trips the
    optimistic lock, so the verifier waits for the job boundary and then reads the
    authoritative revision used by the human decision.
    """
    deadline = time.monotonic() + timeout
    last_status = "missing"
    while time.monotonic() < deadline:
        jobs = request_json_list(
            base_url,
            "GET",
            f"/api/runs/{run_id}/jobs",
            token=token,
        )
        initial_jobs = [job for job in jobs if job.get("stage") == "start"]
        if initial_jobs:
            last_status = str(initial_jobs[-1].get("status", "unknown"))
            if last_status == "failed":
                raise VerificationFailure(
                    f"initial job for run {run_id} failed: "
                    f"{initial_jobs[-1].get('last_error')}"
                )
            if last_status == "succeeded":
                settled = request_json(
                    base_url, "GET", f"/api/runs/{run_id}", token=token
                )
                require(
                    settled.get("status") == "awaiting_approval",
                    (
                        f"run {run_id} changed to {settled.get('status')} after its "
                        "initial job settled"
                    ),
                )
                return settled
        time.sleep(0.2)
    raise VerificationFailure(
        f"initial job for run {run_id} did not settle in {timeout:.0f}s; "
        f"last status={last_status}"
    )


def verify(args: argparse.Namespace) -> dict[str, Any]:
    readiness = request_json(args.base_url, "GET", "/api/ready")
    kube_health = readiness.get("tool_runtime", {}).get("kubernetes_staging", {})
    require(kube_health.get("status") == "ready", "Kubernetes connector is not ready")
    require(
        kube_health.get("write_scope") == ["deployments/scale"],
        f"unexpected Kubernetes write scope: {kube_health.get('write_scope')}",
    )
    admin_token = login(args.base_url, "admin@harbor.local", args.password)
    lead_token = login(args.base_url, "lead@harbor.local", args.password)

    scale_lab(1)
    created = request_json(
        args.base_url,
        "POST",
        "/api/runs",
        token=admin_token,
        payload={
            "incident": {
                "title": "demo-api staging backlog",
                "summary": (
                    "预发布环境队列积压持续升高，请基于真实 Kubernetes 容量信号调查，"
                    "只在审批后执行命名空间内扩容。"
                ),
                "severity": "P2",
                "service": DEPLOYMENT,
                "environment": "staging",
                "symptoms": ["queue depth continues rising"],
            }
        },
        expected_status=202,
    )
    run_id = str(created.get("id", ""))
    require(run_id.startswith("RUN-"), "API did not return a valid run id")

    waiting = poll_for(
        args.base_url,
        run_id,
        admin_token,
        {"awaiting_approval"},
        args.timeout,
    )
    waiting = wait_for_initial_job_settlement(
        args.base_url,
        run_id,
        admin_token,
        args.timeout,
    )
    require(waiting.get("risk_level") == "medium", "scale plan was not medium risk")
    plan = waiting.get("plan", [])
    require(
        plan and plan[0].get("tool_name") == "scale_kubernetes_deployment",
        "Agent did not propose the Kubernetes scale tool",
    )
    require(deployment_state() == (1, 1), "workload changed before human approval")

    approved = request_json(
        args.base_url,
        "POST",
        f"/api/runs/{run_id}/decision",
        token=lead_token,
        payload={
            "decision": "approve",
            "note": "verified evidence and approved namespace-scoped scale",
            "expected_version": waiting["version"],
        },
    )
    require(approved.get("status") == "queued", "approval did not requeue the run")

    completed = poll_for(
        args.base_url,
        run_id,
        admin_token,
        {"completed"},
        args.timeout,
    )
    replicas, ready_replicas = deployment_state()
    require((replicas, ready_replicas) == (4, 4), "real workload did not become 4/4 ready")
    tool_results = completed.get("tool_results", [])
    require(
        any(item.get("tool_name") == "scale_kubernetes_deployment" for item in tool_results),
        "run evidence does not contain the scale execution",
    )
    require(
        any(
            item.get("tool_name") == "inspect_kubernetes_workload"
            and str(item.get("step_id", "")).startswith("step-verify-")
            for item in tool_results
        ),
        "run evidence does not contain independent Kubernetes verification",
    )
    require(
        completed.get("verification")
        and all(item.get("passed") is True for item in completed["verification"]),
        "independent verification did not pass",
    )
    evidence = request_json(
        args.base_url,
        "GET",
        f"/api/runs/{run_id}/evidence",
        token=admin_token,
    )
    require(
        evidence.get("runtime_leak_check", {}).get("passed") is True,
        "evidence leak check did not pass",
    )
    return {
        "status": "passed",
        "run_id": run_id,
        "approval_gate": "passed",
        "pre_approval_replicas": 1,
        "post_approval_replicas": replicas,
        "post_approval_ready_replicas": ready_replicas,
        "independent_verification": "passed",
        "evidence_leak_check": "passed",
        "write_scope": kube_health.get("write_scope"),
        "forbidden_by_design": kube_health.get("forbidden_by_design"),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Verify the complete Agent -> approval -> kind scale -> verify loop."
    )
    result.add_argument("--base-url", default="http://127.0.0.1:8000")
    result.add_argument("--password", default="harbor-demo-2026")
    result.add_argument("--timeout", type=float, default=120.0)
    result.add_argument(
        "--keep-scaled",
        action="store_true",
        help="Keep demo-api at the verified replica count instead of resetting it to one.",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        result = verify(args)
        if not args.keep_scaled:
            scale_lab(1)
            result["lab_reset_to_replicas"] = 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (VerificationFailure, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"kind agent verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
