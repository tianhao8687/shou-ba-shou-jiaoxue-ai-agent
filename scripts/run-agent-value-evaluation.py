from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.evaluation import EvaluationService  # noqa: E402
from app.evaluation_baselines import compare_systems  # noqa: E402
from app.retrieval import create_retriever  # noqa: E402


def atomic_json(path: Path, value: dict) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "data"
        / "evaluation"
        / "evidence"
        / "development-agent-value.json",
    )
    args = parser.parse_args()
    manifest_path = ROOT / "data" / "evaluation" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    split_payload = json.loads(
        (ROOT / "data" / "evaluation" / "development" / "cases.json").read_text(
            encoding="utf-8"
        )
    )
    retriever = create_retriever(
        ROOT / "data", "memory", "sqlite:///unused.db", "lexical-feature-baseline"
    )
    evaluator = EvaluationService(
        manifest_path,
        retriever,
        "agent-value-evaluation-secret-long-enough",
    )
    harbor_report = evaluator.run(
        tenant_id="agent-value-evaluation",
        split="development",
        live_model=False,
    )
    evidence = {
        "format": "harbor-agent-value-evidence/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "dataset_version": manifest["dataset_version"],
        "dataset_split": "development",
        "dataset_hash": manifest["splits"]["development"]["sha256"],
        "model": harbor_report.model_name,
        "retrieval_config_hash": harbor_report.retrieval_config_hash,
        "policy_config_hash": harbor_report.policy_config_hash,
        "run_mode": harbor_report.run_mode,
        **compare_systems(split_payload["cases"], harbor_report),
    }
    atomic_json(args.output.resolve(), evidence)
    print(json.dumps({**evidence["harbor_vs_workflow"], "output": str(args.output.resolve())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
