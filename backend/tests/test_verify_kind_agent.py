from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def load_verifier() -> ModuleType:
    script = Path(__file__).resolve().parents[2] / "scripts" / "verify-kind-agent.py"
    spec = importlib.util.spec_from_file_location("verify_kind_agent", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_waits_for_worker_job_boundary_before_using_approval_version(monkeypatch) -> None:
    verifier = load_verifier()
    job_snapshots = iter(
        [
            [{"stage": "start", "status": "claimed"}],
            [{"stage": "start", "status": "succeeded"}],
        ]
    )
    run_reads: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        verifier,
        "request_json_list",
        lambda *_args, **_kwargs: next(job_snapshots),
    )

    def read_run(base_url: str, method: str, path: str, **_kwargs):
        run_reads.append((base_url, method, path))
        return {"id": "RUN-1", "status": "awaiting_approval", "version": 11}

    monkeypatch.setattr(verifier, "request_json", read_run)
    monkeypatch.setattr(verifier.time, "sleep", lambda _seconds: None)

    settled = verifier.wait_for_initial_job_settlement(
        "http://control-plane",
        "RUN-1",
        "token",
        timeout=1,
    )

    assert settled["version"] == 11
    assert run_reads == [
        ("http://control-plane", "GET", "/api/runs/RUN-1")
    ]
