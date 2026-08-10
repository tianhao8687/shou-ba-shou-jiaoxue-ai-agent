from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.external_validation import (  # noqa: E402
    ExternalValidationError,
    atomic_write_report,
    run_external_validation,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Fetch revision-pinned third-party data, verify hashes, run isolated "
            "holdouts, and write a provenance-bearing evidence report."
        )
    )
    result.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "data" / "external" / "manifest-v1.json",
    )
    result.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "artifacts" / "external-validation" / "raw",
    )
    result.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "external" / "evidence-v1.json",
    )
    result.add_argument("--offline", action="store_true")
    result.add_argument("--timeout", type=float, default=30.0)
    result.add_argument(
        "--require-gates",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return result


def main() -> int:
    args = parser().parse_args()
    if args.timeout <= 0:
        print("timeout must be positive", file=sys.stderr)
        return 2
    try:
        report = run_external_validation(
            args.manifest.resolve(),
            args.cache_dir.resolve(),
            offline=args.offline,
            timeout_seconds=args.timeout,
        )
        atomic_write_report(args.output.resolve(), report)
        summary = {
            "verdict": report.verdict,
            "production_claim": report.production_claim,
            "sources_verified": report.sources_verified,
            "source_files_verified": report.source_files_verified,
            **report.aggregate,
            "evidence_path": str(args.output.resolve()),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if args.require_gates and report.verdict != "pass" else 0
    except (ExternalValidationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"external validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
