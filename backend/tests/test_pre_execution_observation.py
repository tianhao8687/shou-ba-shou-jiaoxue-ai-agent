from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from app.schemas import Check
from app.security import canonical_json
from app.tools import ToolCallResponse


def _prepare_scale_run(
    client: TestClient,
    admin_headers: dict[str, str],
) -> tuple[dict, object]:
    drill_response = client.post(
        "/api/drills",
        headers=admin_headers,
        json={"fault_kind": "queue_backlog"},
    )
    assert drill_response.status_code == 201, drill_response.text
    drill = drill_response.json()
    created = client.post(
        "/api/runs",
        headers=admin_headers,
        json={"incident": drill["incident"]},
    )
    assert created.status_code == 202, created.text
    assert client.app.state.worker.run_once() is True
    record = client.app.state.store.get_run(created.json()["id"])
    assert record is not None
    assert record.status == "awaiting_approval"
    assert record.plan[0].tool_name == "scale_workers"
    record.plan[0].preconditions = [
        Check(
            field="replicas",
            operator="eq",
            value=2,
            description="执行前消费者副本必须仍为两个",
        )
    ]
    serialized = [step.model_dump(mode="json") for step in record.plan]
    rebound_hash = hashlib.sha256(
        canonical_json(serialized).encode("utf-8")
    ).hexdigest()
    record.policy.plan_hash = rebound_hash
    record.approval.plan_hash = rebound_hash
    record.approval.votes = []
    saved = client.app.state.store.save_run(record)
    return drill, saved


def _approve_and_execute(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
    record,
) -> dict:
    approved = client.post(
        f"/api/runs/{record.id}/decision",
        headers=lead_headers,
        json={
            "decision": "approve",
            "note": "已确认计划哈希，执行前必须重新读取真实状态。",
            "expected_version": record.version,
        },
    )
    assert approved.status_code == 200, approved.text
    assert client.app.state.worker.run_once() is True
    response = client.get(f"/api/runs/{record.id}", headers=admin_headers)
    assert response.status_code == 200
    return response.json()


def _write_results(run: dict) -> list[dict]:
    return [
        result
        for result in run["tool_results"]
        if result["tool_name"] == "scale_workers"
    ]


def test_approval_to_execution_state_drift_blocks_write(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
) -> None:
    drill, record = _prepare_scale_run(client, admin_headers)
    lab = client.app.state.inprocess_lab
    assert lab._experiments[drill["experiment_id"]]["state"]["replicas"] == 2
    lab._experiments[drill["experiment_id"]]["state"]["replicas"] = 4

    handed_off = _approve_and_execute(
        client, admin_headers, lead_headers, record
    )

    assert handed_off["status"] == "handed_off"
    assert handed_off["error_code"] == "PRECONDITION_CHANGED"
    assert _write_results(handed_off) == []
    assert lab._experiments[drill["experiment_id"]]["effects"] == 0
    assert any(
        event["action"] == "precondition.changed"
        and event["metadata"]["result"] == "changed"
        for event in handed_off["audit"]
    )


def test_unchanged_fresh_state_allows_approved_write(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
) -> None:
    drill, record = _prepare_scale_run(client, admin_headers)

    completed = _approve_and_execute(client, admin_headers, lead_headers, record)

    assert completed["status"] == "completed"
    assert len(_write_results(completed)) == 1
    assert client.app.state.inprocess_lab._experiments[drill["experiment_id"]][
        "effects"
    ] == 1
    actions = [event["action"] for event in completed["audit"]]
    assert "precondition.revalidation.started" in actions
    assert "precondition.revalidation.succeeded" in actions
    assert "tool.execution.started" in actions
    assert "tool.execution.succeeded" in actions


def test_pre_execution_observation_timeout_fails_closed(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
    monkeypatch,
) -> None:
    drill, record = _prepare_scale_run(client, admin_headers)
    lab = client.app.state.inprocess_lab
    original_invoke = lab.invoke

    def timeout(tool_name, *args, **kwargs):
        if tool_name == "get_service_status":
            raise TimeoutError("fresh state read timed out")
        return original_invoke(tool_name, *args, **kwargs)

    monkeypatch.setattr(lab, "invoke", timeout)
    handed_off = _approve_and_execute(
        client, admin_headers, lead_headers, record
    )

    assert handed_off["status"] == "handed_off"
    assert handed_off["error_code"] == "PRE_EXECUTION_OBSERVATION_FAILED"
    assert _write_results(handed_off) == []
    assert lab._experiments[drill["experiment_id"]]["effects"] == 0


def test_invalid_fresh_observation_never_falls_back_to_stale_state(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
    monkeypatch,
) -> None:
    drill, record = _prepare_scale_run(client, admin_headers)
    lab = client.app.state.inprocess_lab
    original_invoke = lab.invoke

    def invalid(tool_name, *args, **kwargs):
        if tool_name == "get_service_status":
            return ToolCallResponse(
                "succeeded",
                "malformed observer response",
                {"replicas": "two"},
            )
        return original_invoke(tool_name, *args, **kwargs)

    monkeypatch.setattr(lab, "invoke", invalid)
    handed_off = _approve_and_execute(
        client, admin_headers, lead_headers, record
    )

    assert handed_off["status"] == "handed_off"
    assert handed_off["error_code"] == "PRE_EXECUTION_OBSERVATION_INVALID"
    assert _write_results(handed_off) == []
    assert lab._experiments[drill["experiment_id"]]["effects"] == 0
