from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.evaluation import EvaluationService  # noqa: E402
from app.model_adapter import create_model_adapter  # noqa: E402
from app.retrieval import create_retriever  # noqa: E402


class SuiteFailure(RuntimeError):
    pass


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
    retriever = create_retriever(
        ROOT / "data",
        "memory",
        "sqlite:///unused.db",
        "lexical-feature-baseline",
        "",
        "",
    )
    live = args.mode == "live"
    adapter = (
        create_model_adapter(
            enabled=True,
            endpoint=args.endpoint,
            model=args.model,
            provider="local-openvino",
            timeout_seconds=args.timeout,
            api_key=args.api_key,
        )
        if live
        else None
    )
    evaluator = EvaluationService(
        ROOT / "data" / "evaluation" / "manifest.json",
        retriever,
        "harbor-offline-evaluation-capability-secret",
        adapter,
    )
    reports = [
        evaluator.run(
            tenant_id="offline-evaluation",
            live_model=live,
            case_limit=args.case_limit,
            case_offset=args.case_offset,
            split=args.split,
            allow_frozen_holdout=args.final_holdout,
        )
        for _ in range(args.repetitions)
    ]
    total = sum(report.case_count for report in reports)
    passed = sum(report.passed_count for report in reports)
    unsafe = sum(
        1 for report in reports for case in report.cases if case.unsafe_action
    )
    if total == 0:
        raise SuiteFailure("evaluation produced no cases")
    if args.require_perfect and passed != total:
        failures = [
            case.case_id
            for report in reports
            for case in report.cases
            if not case.passed
        ]
        raise SuiteFailure(f"{total - passed} cases failed: {failures[:20]}")
    result = {
        "format": "harbor-evaluation-evidence/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "configuration": {
            "mode": args.mode,
            "repetitions": args.repetitions,
            "case_limit": args.case_limit,
            "case_offset": args.case_offset,
            "dataset_split": args.split,
            "final_holdout_unlock": args.final_holdout,
            "model": args.model if live else "transparent-heuristic-fixture",
            "oracle_visibility": "post-run-only",
            "case_state_isolation": "new experiment and database per case",
        },
        "aggregate": {
            "suite_version": reports[0].suite_version,
            "suite_fingerprint": reports[0].suite_fingerprint,
            "case_trials": total,
            "passed_trials": passed,
            "pass_rate": round(passed * 100 / total, 2),
            "unsafe_actions": unsafe,
            "unsafe_action_rate": round(unsafe * 100 / total, 2),
            "all_required_cases_passed": passed == total,
            "reported_wilson_interval": {
                "confidence_level": reports[0].confidence_level,
                "lower": min(report.task_success_ci_lower for report in reports),
                "upper": max(report.task_success_ci_upper for report in reports),
            },
        },
        "reports": [report.model_dump(mode="json") for report in reports],
    }
    atomic_json(args.output.resolve(), result)
    return {
        **result["aggregate"],
        "status": "passed" if passed == total else "failed",
        "evidence_path": str(args.output.resolve()),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Run the sealed v4 suite and persist reproducible evidence."
    )
    result.add_argument("--mode", choices=("fixture", "live"), default="fixture")
    result.add_argument("--repetitions", type=int, default=1)
    result.add_argument("--case-limit", type=int)
    result.add_argument("--case-offset", type=int, default=0)
    result.add_argument(
        "--split",
        choices=("calibration", "development", "holdout"),
        default="development",
    )
    result.add_argument("--final-holdout", action="store_true")
    result.add_argument("--endpoint", default="http://127.0.0.1:8091/v1")
    result.add_argument("--model", default="OpenVINO/Qwen3-VL-8B-Instruct-int4-ov")
    result.add_argument("--api-key", default="harbor-local-model-token")
    result.add_argument("--timeout", type=float, default=240.0)
    result.add_argument(
        "--require-perfect",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    result.add_argument(
        "--output",
        type=Path,
        default=ROOT / "docs" / "evaluation-evidence-v4.json",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    if args.repetitions < 1 or args.case_offset < 0:
        print("repetitions must be >= 1 and offset must be >= 0", file=sys.stderr)
        return 2
    if args.split == "holdout" and not args.final_holdout:
        print("holdout requires --final-holdout and must not be used for tuning", file=sys.stderr)
        return 2
    try:
        summary = execute(args)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (SuiteFailure, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"evaluation suite failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
