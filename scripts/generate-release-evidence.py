from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any


REQUIRED_JOBS = (
    "Quality gates",
    "Pinned external data validation",
    "Backend tests",
    "Frontend tests",
    "Compose smoke and browser E2E",
    "kind least-privilege staging E2E",
)


class EvidenceError(RuntimeError):
    pass


def gh_json(*arguments: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["gh", *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise EvidenceError(completed.stderr.strip() or "GitHub CLI request failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise EvidenceError("GitHub CLI did not return JSON") from exc
    if not isinstance(payload, dict):
        raise EvidenceError("GitHub CLI returned an unexpected JSON shape")
    return payload


def repository_name(explicit: str | None) -> str:
    if explicit:
        return explicit
    payload = gh_json("repo", "view", "--json", "nameWithOwner")
    value = payload.get("nameWithOwner")
    if not isinstance(value, str) or "/" not in value:
        raise EvidenceError("could not resolve the current GitHub repository")
    return value


def collect_evidence(
    repository: str,
    run_id: int,
    expected_commit: str | None,
) -> dict[str, Any]:
    run = gh_json("api", f"repos/{repository}/actions/runs/{run_id}")
    jobs_payload = gh_json(
        "api", f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100"
    )
    jobs = jobs_payload.get("jobs")
    if not isinstance(jobs, list):
        raise EvidenceError("GitHub Actions jobs response is missing jobs")

    commit = run.get("head_sha")
    if not isinstance(commit, str) or len(commit) != 40:
        raise EvidenceError("workflow run is missing a full head SHA")
    if expected_commit and commit != expected_commit:
        raise EvidenceError(
            f"workflow run commit {commit} does not match {expected_commit}"
        )

    by_name = {
        str(job.get("name")): job
        for job in jobs
        if isinstance(job, dict) and isinstance(job.get("name"), str)
    }
    missing = [name for name in REQUIRED_JOBS if name not in by_name]
    failed = [
        name
        for name in REQUIRED_JOBS
        if name in by_name and by_name[name].get("conclusion") != "success"
    ]
    if run.get("conclusion") != "success" or missing or failed:
        raise EvidenceError(
            "refusing to publish non-green evidence: "
            f"run={run.get('conclusion')}, missing={missing}, non_success={failed}"
        )

    required_jobs = {
        name: {
            "status": "passed",
            "job_id": by_name[name].get("id"),
            "url": by_name[name].get("html_url"),
            "started_at": by_name[name].get("started_at"),
            "completed_at": by_name[name].get("completed_at"),
        }
        for name in REQUIRED_JOBS
    }
    return {
        "format": "harbor-agentops-release-evidence/v1",
        "version": "3.6.0",
        "repository": repository,
        "validated_code_commit": commit,
        "ci": {
            "status": "passed",
            "run_id": run_id,
            "url": run.get("html_url"),
            "workflow": run.get("name"),
            "event": run.get("event"),
            "created_at": run.get("created_at"),
            "validated_at": run.get("updated_at"),
            "required_jobs_passed": len(REQUIRED_JOBS),
            "required_jobs_total": len(REQUIRED_JOBS),
        },
        "required_ci_jobs": required_jobs,
        "production_claim": False,
        "claim_boundary": (
            "This proves the repository's required CI gates for the recorded commit; "
            "it does not prove sustained enterprise production traffic."
        ),
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(encoded)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate release evidence from an actually green GitHub Actions run."
    )
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--repository")
    parser.add_argument("--expected-commit")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/release-evidence-v3.6.json"),
    )
    args = parser.parse_args()
    try:
        payload = collect_evidence(
            repository_name(args.repository),
            args.run_id,
            args.expected_commit,
        )
        atomic_json(args.output, payload)
    except EvidenceError as exc:
        print(f"release evidence generation failed: {exc}", file=os.sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
