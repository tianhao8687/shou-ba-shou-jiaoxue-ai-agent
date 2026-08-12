from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from app.schemas import Check, PlanStep, RiskLevel, RollbackPlan
from app.security import canonical_json
from app.tools import ToolCallResponse


def _prepare_two_step_run(
    client: TestClient,
    admin_headers: dict[str, str],
    *,
    read_only_first: bool = False,
):
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
    base = record.plan[0]
    common = {
        "experiment_id": drill["experiment_id"],
        "service": drill["incident"]["service"],
    }
    if read_only_first:
        step_a = PlanStep(
            id="step-read-current-metrics",
            title="读取当前队列指标",
            objective="只读确认当前队列指标，不改变任何外部状态。",
            tool_name="query_metrics",
            tool_input={**common, "window_minutes": 5},
            evidence_ids=base.evidence_ids,
            success_criteria=[
                Check(
                    field="__result_status__",
                    operator="eq",
                    value="succeeded",
                    description="只读指标调用成功",
                )
            ],
            rollback=RollbackPlan(
                mode="manual", rationale="只读步骤没有需要撤销的外部副作用。"
            ),
            risk=RiskLevel.LOW,
            rationale="用于证明只读步骤不会错误进入 Saga 补偿集合。",
        )
    else:
        step_a = base.model_copy(
            update={
                "id": "step-scale-stage-a",
                "title": "第一阶段受控扩容",
                "tool_input": {
                    **common,
                    "target_replicas": 3,
                    "change_ticket": "CHG-3001",
                },
                "preconditions": [
                    Check(
                        field="replicas",
                        operator="eq",
                        value=2,
                        description="第一阶段执行前必须仍是两个副本",
                    )
                ],
                "success_criteria": [
                    Check(
                        field="replicas",
                        operator="eq",
                        value=3,
                        description="第一阶段扩到三个副本",
                    )
                ],
                "rollback": RollbackPlan(
                    mode="tool",
                    tool_name="scale_workers",
                    tool_input={
                        **common,
                        "target_replicas": 2,
                        "change_ticket": "CHG-3001",
                    },
                    rationale="失败时恢复到第一阶段执行前的两个副本。",
                ),
            }
        )
    step_b = base.model_copy(
        update={
            "id": "step-scale-stage-b",
            "title": "第二阶段受控扩容",
            "depends_on": [step_a.id],
            "tool_input": {
                **common,
                "target_replicas": 6,
                "change_ticket": "CHG-3001",
            },
            "preconditions": (
                []
                if read_only_first
                else [
                    Check(
                        field="replicas",
                        operator="eq",
                        value=3,
                        description="第二阶段执行前必须是三个副本",
                    )
                ]
            ),
            "rollback": RollbackPlan(
                mode="tool",
                tool_name="scale_workers",
                tool_input={
                    **common,
                    "target_replicas": 3 if not read_only_first else 2,
                    "change_ticket": "CHG-3001",
                },
                rationale="失败时恢复到第二阶段执行前的副本数。",
            ),
        }
    )
    record.plan = [step_a, step_b]
    serialized = [step.model_dump(mode="json") for step in record.plan]
    rebound_hash = hashlib.sha256(
        canonical_json(serialized).encode("utf-8")
    ).hexdigest()
    record.policy.plan_hash = rebound_hash
    record.approval.plan_hash = rebound_hash
    record.approval.votes = []
    return drill, client.app.state.store.save_run(record)


def _approve_and_run(
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
            "note": "批准两个受控步骤和工具合同生成的逆序补偿。",
            "expected_version": record.version,
        },
    )
    assert approved.status_code == 200, approved.text
    assert client.app.state.worker.run_once() is True
    response = client.get(f"/api/runs/{record.id}", headers=admin_headers)
    assert response.status_code == 200
    return response.json()


def test_two_successful_writes_continue_to_verification(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
) -> None:
    drill, record = _prepare_two_step_run(client, admin_headers)

    completed = _approve_and_run(client, admin_headers, lead_headers, record)

    assert completed["status"] == "completed"
    assert [item["status"] for item in completed["compensations"]] == [
        "ready",
        "ready",
    ]
    assert not any(
        event["action"].startswith("compensation.")
        for event in completed["audit"]
    )
    assert client.app.state.inprocess_lab._experiments[
        drill["experiment_id"]
    ]["state"]["replicas"] == 6


def test_partial_failure_compensates_prior_success_in_reverse(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
    monkeypatch,
) -> None:
    drill, record = _prepare_two_step_run(client, admin_headers)
    lab = client.app.state.inprocess_lab
    original_apply = lab._apply

    def fail_second(tool_name, payload, experiment):
        if tool_name == "scale_workers" and payload["target_replicas"] == 6:
            return ToolCallResponse(
                "failed", "第二阶段被故障注入明确拒绝。", {}, "injected_failure"
            )
        return original_apply(tool_name, payload, experiment)

    monkeypatch.setattr(lab, "_apply", fail_second)
    failed = _approve_and_run(client, admin_headers, lead_headers, record)

    assert failed["status"] == "failed"
    assert failed["error_code"] == "PARTIAL_FAILURE_COMPENSATED"
    assert lab._experiments[drill["experiment_id"]]["state"]["replicas"] == 2
    assert [item["status"] for item in failed["compensations"]] == [
        "succeeded",
        "not_required",
    ]
    actions = [event["action"] for event in failed["audit"]]
    assert "compensation.started" in actions
    assert "compensation.succeeded" in actions


def test_compensation_failure_hands_off_with_partial_state(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
    monkeypatch,
) -> None:
    drill, record = _prepare_two_step_run(client, admin_headers)
    lab = client.app.state.inprocess_lab
    original_apply = lab._apply

    def fail_second_and_compensation(tool_name, payload, experiment):
        if tool_name == "scale_workers" and payload["target_replicas"] in {2, 6}:
            return ToolCallResponse(
                "failed", "故障注入拒绝写操作。", {}, "injected_failure"
            )
        return original_apply(tool_name, payload, experiment)

    monkeypatch.setattr(lab, "_apply", fail_second_and_compensation)
    handed_off = _approve_and_run(client, admin_headers, lead_headers, record)

    assert handed_off["status"] == "handed_off"
    assert handed_off["error_code"] == "COMPENSATION_FAILED"
    assert lab._experiments[drill["experiment_id"]]["state"]["replicas"] == 3
    assert handed_off["compensations"][0]["manual_intervention_required"] is True
    assert any(
        event["action"] == "compensation.failed"
        for event in handed_off["audit"]
    )


def test_unknown_second_write_freezes_chain_without_compensation_or_retry(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
    monkeypatch,
) -> None:
    drill, record = _prepare_two_step_run(client, admin_headers)
    lab = client.app.state.inprocess_lab
    original_apply = lab._apply

    def lose_ack_for_second(tool_name, payload, experiment):
        if tool_name == "scale_workers" and payload["target_replicas"] == 6:
            raise TimeoutError("write outcome is unknown")
        return original_apply(tool_name, payload, experiment)

    monkeypatch.setattr(lab, "_apply", lose_ack_for_second)
    handed_off = _approve_and_run(client, admin_headers, lead_headers, record)

    assert handed_off["status"] == "handed_off"
    assert handed_off["error_code"] == "TOOL_EXECUTION_UNKNOWN"
    assert lab._experiments[drill["experiment_id"]]["state"]["replicas"] == 3
    assert [item["status"] for item in handed_off["compensations"]] == [
        "ready",
        "unknown",
    ]
    assert not any(
        event["action"] == "compensation.started"
        for event in handed_off["audit"]
    )
    second_results = [
        item
        for item in handed_off["tool_results"]
        if item["step_id"] == "step-scale-stage-b"
    ]
    assert len(second_results) == 1


def test_read_only_success_is_not_compensated_when_later_write_fails(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
    monkeypatch,
) -> None:
    _drill, record = _prepare_two_step_run(
        client, admin_headers, read_only_first=True
    )
    lab = client.app.state.inprocess_lab
    original_apply = lab._apply

    def fail_write(tool_name, payload, experiment):
        if tool_name == "scale_workers":
            return ToolCallResponse(
                "failed", "写操作被明确拒绝。", {}, "injected_failure"
            )
        return original_apply(tool_name, payload, experiment)

    monkeypatch.setattr(lab, "_apply", fail_write)
    failed = _approve_and_run(client, admin_headers, lead_headers, record)

    assert failed["status"] == "failed"
    assert failed["error_code"] == "TOOL_EXECUTION_FAILED"
    assert len(failed["compensations"]) == 1
    assert failed["compensations"][0]["status"] == "not_required"
    assert not any(
        event["action"] == "compensation.started"
        for event in failed["audit"]
    )
