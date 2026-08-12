from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_reliability_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "reliability-lab.py"
    spec = importlib.util.spec_from_file_location("reliability_lab", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_http_timeout_is_normalized_for_fault_recovery_polling(monkeypatch) -> None:
    module = _load_reliability_module()

    def timeout(*_args, **_kwargs):
        raise TimeoutError("dependency restart window")

    monkeypatch.setattr(module, "urlopen", timeout)

    with pytest.raises(module.ReliabilityFailure, match="dependency restart window"):
        module.http_json("http://127.0.0.1:8000", "GET", "/api/ready")


def test_claim_poll_uses_database_probe_without_reading_locked_run(monkeypatch) -> None:
    module = _load_reliability_module()
    moments = iter((0.0, 0.1, 0.2))
    responses = iter(
        (
            "",
            '{"id":"JOB-1","status":"claimed","fencing_token":4,"owner":"worker-1"}',
        )
    )
    monkeypatch.setattr(module.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(module, "compose", lambda *_args: next(responses))

    claimed = module.poll_claimed_job(SimpleNamespace(), "RUN-ABC123", timeout=30)

    assert claimed == {
        "id": "JOB-1",
        "status": "claimed",
        "fencing_token": 4,
        "owner": "worker-1",
    }


def test_claim_poll_rejects_non_generated_run_id() -> None:
    module = _load_reliability_module()

    with pytest.raises(module.ReliabilityFailure, match="unsafe generated run id"):
        module.poll_claimed_job(SimpleNamespace(), "RUN-X'; DROP TABLE agent_jobs;--")
