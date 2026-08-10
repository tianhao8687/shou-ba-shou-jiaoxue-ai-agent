from __future__ import annotations

import json
from pathlib import Path
import sys


THRESHOLDS = {
    "TOTAL": 82.0,
    "app/store.py": 75.0,
    "app/worker.py": 75.0,
}


def main() -> int:
    report_path = Path(sys.argv[1] if len(sys.argv) > 1 else "backend/coverage.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    files = {
        str(module).replace("\\", "/"): details
        for module, details in report["files"].items()
    }
    missing_modules = [
        module for module in THRESHOLDS if module != "TOTAL" and module not in files
    ]
    if missing_modules:
        print(
            "Coverage report is missing required modules: " + ", ".join(missing_modules),
            file=sys.stderr,
        )
        return 1
    observed = {
        "TOTAL": float(report["totals"]["percent_covered"]),
        **{
            module: float(files[module]["summary"]["percent_covered"])
            for module in THRESHOLDS
            if module != "TOTAL"
        },
    }
    failures = [
        f"{name}: {observed[name]:.2f}% < {minimum:.2f}%"
        for name, minimum in THRESHOLDS.items()
        if observed[name] < minimum
    ]
    for name, minimum in THRESHOLDS.items():
        print(f"{name}: {observed[name]:.2f}% (required {minimum:.2f}%)")
    if failures:
        print("Coverage gate failed: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("Coverage gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
