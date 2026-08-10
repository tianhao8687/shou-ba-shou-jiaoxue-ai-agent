from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-coverage.py"


def _run_gate(tmp_path: Path, files: dict[str, float]) -> subprocess.CompletedProcess[str]:
    report = {
        "totals": {"percent_covered": 83.0},
        "files": {
            path: {"summary": {"percent_covered": percentage}}
            for path, percentage in files.items()
        },
    }
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(path)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_coverage_gate_accepts_windows_report_paths(tmp_path: Path) -> None:
    result = _run_gate(
        tmp_path,
        {"app\\store.py": 76.0, "app\\worker.py": 80.0},
    )

    assert result.returncode == 0, result.stderr
    assert "Coverage gate passed" in result.stdout


def test_coverage_gate_fails_closed_when_a_required_module_is_missing(
    tmp_path: Path,
) -> None:
    result = _run_gate(tmp_path, {"app/store.py": 76.0})

    assert result.returncode == 1
    assert "app/worker.py" in result.stderr
