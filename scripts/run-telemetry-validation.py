from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.telemetry_validation import (  # noqa: E402
    TelemetryPredictionsArtifact,
    TelemetryValidationError,
    atomic_write_telemetry_report,
    run_telemetry_validation,
    telemetry_semantic_fingerprint,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Verify pinned AIOps2025 archives, extract real logs/metrics/traces, "
            "freeze deterministic RCA predictions, then open the oracle and score."
        )
    )
    result.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "data" / "external" / "telemetry-manifest-v4.json",
    )
    result.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "artifacts" / "external-validation" / "raw",
    )
    result.add_argument(
        "--predictions",
        type=Path,
        default=ROOT / ".runtime" / "telemetry-predictions.json",
    )
    result.add_argument(
        "--output",
        type=Path,
        default=ROOT / ".runtime" / "telemetry-evidence.json",
    )
    result.add_argument(
        "--assert-semantic-equals",
        type=Path,
        help=(
            "Fail unless the new predictions have the same canonical content as "
            "this artifact; generated_at and latency_ms are intentionally excluded."
        ),
    )
    result.add_argument("--offline", action="store_true")
    result.add_argument("--timeout", type=float, default=120.0)
    result.add_argument(
        "--require-gates",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Return exit code 1 when a quality gate fails. Integrity failures always fail.",
    )
    return result


def main() -> int:
    args = parser().parse_args()
    if args.timeout <= 0:
        print("timeout must be positive", file=sys.stderr)
        return 2
    predictions_path = args.predictions.resolve()
    comparison_path = (
        args.assert_semantic_equals.resolve()
        if args.assert_semantic_equals is not None
        else None
    )
    if comparison_path == predictions_path:
        print(
            "--assert-semantic-equals must not point to the output prediction file",
            file=sys.stderr,
        )
        return 2
    try:
        report = run_telemetry_validation(
            args.manifest.resolve(),
            args.cache_dir.resolve(),
            predictions_path,
            offline=args.offline,
            timeout_seconds=args.timeout,
        )
        artifact = TelemetryPredictionsArtifact.model_validate_json(
            predictions_path.read_bytes()
        )
        semantic_fingerprint = telemetry_semantic_fingerprint(artifact)
        if comparison_path is not None:
            expected = TelemetryPredictionsArtifact.model_validate_json(
                comparison_path.read_bytes()
            )
            expected_fingerprint = telemetry_semantic_fingerprint(expected)
            if semantic_fingerprint != expected_fingerprint:
                raise TelemetryValidationError(
                    "semantic replay mismatch: "
                    f"expected {expected_fingerprint}, got {semantic_fingerprint}"
                )
        atomic_write_telemetry_report(args.output.resolve(), report)
        print(
            json.dumps(
                {
                    "verdict": report.verdict,
                    "production_claim": report.production_claim,
                    "all_rows": report.coverage["all_rows"],
                    "calibration": report.calibration,
                    "holdout": report.holdout,
                    "passed_gates": sum(item.passed for item in report.gates),
                    "gate_count": len(report.gates),
                    "prediction_fingerprint": report.prediction_fingerprint,
                    "semantic_prediction_fingerprint": semantic_fingerprint,
                    "predictions_path": str(predictions_path),
                    "evidence_path": str(args.output.resolve()),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if args.require_gates and report.verdict != "pass" else 0
    except (TelemetryValidationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"telemetry validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
