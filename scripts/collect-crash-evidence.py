from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(encoded)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def collect(junit_path: Path, manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = ET.parse(junit_path).getroot()
    tests = {case.attrib.get("name", ""): case for case in root.iter("testcase")}
    results = []
    for declared in manifest["cases"]:
        testcase = tests.get(declared["test"])
        if testcase is None:
            status = "missing"
            detail = "test was not present in JUnit evidence"
        elif testcase.find("failure") is not None or testcase.find("error") is not None:
            status = "failed"
            node = testcase.find("failure") or testcase.find("error")
            detail = (node.text or "")[:500]
        elif testcase.find("skipped") is not None:
            status = "skipped"
            detail = testcase.find("skipped").attrib.get("message", "")
        else:
            status = "passed"
            detail = ""
        results.append({**declared, "status": status, "detail": detail})
    unacceptable = [item for item in results if item["status"] in {"missing", "failed"}]
    return {
        "format": "harbor-crash-window-evidence/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "status": "failed" if unacceptable else "passed",
        "passed": sum(item["status"] == "passed" for item in results),
        "skipped": sum(item["status"] == "skipped" for item in results),
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "data" / "reliability" / "crash-window-manifest.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = collect(args.junit, args.manifest)
    atomic_json(args.output, evidence)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
