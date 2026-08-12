from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.onboarding import register_warehouse_sync
from app.registry import RUNBOOK_REGISTRY, SERVICE_REGISTRY
from app.tools import TOOL_REGISTRY


ROOT = Path(__file__).resolve().parents[1]


def agent_core_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "app" / "agent").rglob("*.py")):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def test_warehouse_sync_onboards_and_runs_without_modifying_agent_core(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
) -> None:
    before = agent_core_digest()
    register_warehouse_sync(replace=True)
    after = agent_core_digest()

    assert after == before
    service = SERVICE_REGISTRY["warehouse-sync"]
    assert service.runbook_ids == ("RB-WS-501",)
    assert service.allowed_observations == ("inspect_sync_lag",)
    assert {"inspect_sync_lag", "replay_sync_checkpoint"} <= set(
        service.allowed_tools
    )
    assert "RB-WS-501" in RUNBOOK_REGISTRY
    assert TOOL_REGISTRY["inspect_sync_lag"].read_only is True
    assert TOOL_REGISTRY["replay_sync_checkpoint"].read_only is False
    assert TOOL_REGISTRY["replay_sync_checkpoint"].idempotency_policy == "hash-bound"

    drill_response = client.post(
        "/api/drills",
        json={"fault_kind": "warehouse_checkpoint_lag"},
        headers=admin_headers,
    )
    assert drill_response.status_code == 201, drill_response.text
    drill = drill_response.json()
    run_response = client.post(
        "/api/runs",
        json={"incident": drill["incident"]},
        headers=admin_headers,
    )
    assert run_response.status_code == 202, run_response.text
    run_id = run_response.json()["id"]

    assert client.app.state.worker.run_once() is True
    waiting_response = client.get(f"/api/runs/{run_id}", headers=admin_headers)
    waiting = waiting_response.json()
    assert waiting["status"] == "awaiting_approval"
    assert [step["tool_name"] for step in waiting["investigation_plan"]] == [
        "inspect_sync_lag"
    ]
    assert waiting["plan"][0]["tool_name"] == "replay_sync_checkpoint"
    assert waiting["approval"]["required_approvals"] == 1
    assert any(source["doc_id"] == "RB-WS-501" for source in waiting["sources"])

    approval = client.post(
        f"/api/runs/{run_id}/decision",
        json={
            "decision": "approve",
            "note": "已核对分区、目标检查点、计划哈希与人工恢复路径。",
            "expected_version": waiting["version"],
        },
        headers=lead_headers,
    )
    assert approval.status_code == 200, approval.text
    assert client.app.state.worker.run_once() is True

    completed = client.get(f"/api/runs/{run_id}", headers=admin_headers).json()
    assert completed["status"] == "completed"
    writes = [
        result
        for result in completed["tool_results"]
        if result["tool_name"] == "replay_sync_checkpoint"
    ]
    assert len(writes) == 1
    assert writes[0]["capability_jti"].startswith("CAP-")
    assert completed["verification"][0]["passed"] is True
    assert any(
        result["tool_name"] == "inspect_sync_lag"
        and result["step_id"].startswith("step-verify-")
        for result in completed["tool_results"]
    )
    assert client.app.state.inprocess_lab.get_state(drill["experiment_id"])["effects"] == 1
