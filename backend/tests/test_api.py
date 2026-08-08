from __future__ import annotations

from fastapi.testclient import TestClient

from app.tools import ToolCallResponse


def create_drill(client: TestClient, headers: dict[str, str], fault_kind: str) -> dict:
    response = client.post("/api/drills", json={"fault_kind": fault_kind}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def start(client: TestClient, headers: dict[str, str], incident: dict) -> dict:
    response = client.post("/api/runs", json={"incident": incident}, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()


def get_run(client: TestClient, headers: dict[str, str], run_id: str) -> dict:
    response = client.get(f"/api/runs/{run_id}", headers=headers)
    assert response.status_code == 200
    return response.json()


def test_api_requires_signed_identity(client: TestClient) -> None:
    assert client.get("/api/runs").status_code == 401
    assert client.post("/api/drills", json={"fault_kind": "stale_cache"}).status_code == 401


def test_observer_cannot_start_or_cancel_operational_work(
    client: TestClient,
    admin_headers: dict[str, str],
    viewer_headers: dict[str, str],
) -> None:
    drill = create_drill(client, admin_headers, "stale_cache")
    blocked_start = client.post(
        "/api/runs", json={"incident": drill["incident"]}, headers=viewer_headers
    )
    assert blocked_start.status_code == 403

    run = start(client, admin_headers, drill["incident"])
    blocked_cancel = client.post(
        f"/api/runs/{run['id']}/cancel?expected_version={run['version']}",
        headers=viewer_headers,
    )
    assert blocked_cancel.status_code == 403


def test_health_exposes_truthful_vector_and_worker_contract(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == "3.2.0"
    assert payload["vector_quality"] == "lexical-feature-baseline"
    assert "feature-hashing" in payload["vector_backend"]
    assert payload["worker_runtime"]["status"] == "external"
    assert payload["worker_runtime"]["mode"] == "standalone-process"
    assert payload["model_runtime"]["status"] == "fixture"


def test_high_risk_run_is_queued_gated_authorized_and_verified(
    client: TestClient,
    admin_headers: dict[str, str],
    viewer_headers: dict[str, str],
    lead_headers: dict[str, str],
    approver_headers: dict[str, str],
) -> None:
    drill = create_drill(client, admin_headers, "connection_pool_exhaustion")
    run = start(client, admin_headers, drill["incident"])
    assert run["status"] == "queued"
    assert client.app.state.worker.run_once() is True
    waiting = get_run(client, admin_headers, run["id"])
    assert waiting["status"] == "awaiting_approval"
    assert waiting["policy"]["accepted"] is True
    assert waiting["policy"]["plan_hash"] == waiting["approval"]["plan_hash"]
    assert waiting["approval"]["required_approvals"] == 2
    assert waiting["approval"]["separation_of_duties"] is True
    assert waiting["approval"]["requester"] == "admin@harbor.local"
    assert waiting["approval"]["votes"] == []
    assert not any(
        result["tool_name"] == "restart_service" for result in waiting["tool_results"]
    )

    denied_role = client.post(
        f"/api/runs/{run['id']}/decision",
        json={
            "decision": "approve",
            "note": "viewer must not approve",
            "expected_version": waiting["version"],
        },
        headers=viewer_headers,
    )
    assert denied_role.status_code == 403

    requester_vote = client.post(
        f"/api/runs/{run['id']}/decision",
        json={
            "decision": "approve",
            "note": "申请人不能审批自己的变更",
            "expected_version": waiting["version"],
        },
        headers=admin_headers,
    )
    assert requester_vote.status_code == 403

    first_vote = client.post(
        f"/api/runs/{run['id']}/decision",
        json={
            "decision": "approve",
            "note": "一审已核对单实例范围和人工回滚",
            "expected_version": waiting["version"],
        },
        headers=lead_headers,
    )
    assert first_vote.status_code == 200, first_vote.text
    pending = first_vote.json()
    assert pending["status"] == "awaiting_approval"
    assert pending["approval"]["decision"] == "pending"
    assert len(pending["approval"]["votes"]) == 1
    assert pending["approval"]["votes"][0]["plan_hash"] == pending["policy"]["plan_hash"]
    assert not any(
        result["tool_name"] == "restart_service" for result in pending["tool_results"]
    )

    duplicate_vote = client.post(
        f"/api/runs/{run['id']}/decision",
        json={
            "decision": "approve",
            "note": "同一主体不能重复投票",
            "expected_version": pending["version"],
        },
        headers=lead_headers,
    )
    assert duplicate_vote.status_code == 409

    approved = client.post(
        f"/api/runs/{run['id']}/decision",
        json={
            "decision": "approve",
            "note": "二审确认计划哈希、影响面和回滚路径",
            "expected_version": pending["version"],
        },
        headers=approver_headers,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "queued"
    assert len(approved.json()["approval"]["votes"]) == 2
    assert {vote["approver"] for vote in approved.json()["approval"]["votes"]} == {
        "lead@harbor.local",
        "approver@harbor.local",
    }
    assert all(
        vote["plan_hash"] == approved.json()["policy"]["plan_hash"]
        for vote in approved.json()["approval"]["votes"]
    )
    assert client.app.state.worker.run_once() is True
    completed = get_run(client, admin_headers, run["id"])
    assert completed["status"] == "completed"
    writes = [
        item for item in completed["tool_results"] if item["tool_name"] == "restart_service"
    ]
    assert len(writes) == 1
    assert writes[0]["capability_jti"].startswith("CAP-")
    assert completed["verification"][0]["passed"] is True
    assert any(item["tool_name"] == "query_metrics" for item in completed["tool_results"])


def test_denial_prevents_sensitive_tool_execution(
    client: TestClient,
    admin_headers: dict[str, str],
    security_headers: dict[str, str],
) -> None:
    drill = create_drill(client, admin_headers, "expired_credential")
    run = start(client, admin_headers, drill["incident"])
    client.app.state.worker.run_once()
    waiting = get_run(client, admin_headers, run["id"])
    response = client.post(
        f"/api/runs/{run['id']}/decision",
        json={
            "decision": "deny",
            "note": "证据需要人工复核",
            "expected_version": waiting["version"],
        },
        headers=security_headers,
    )
    assert response.status_code == 200
    denied = response.json()
    assert denied["status"] == "handed_off"
    assert not any(result["tool_name"] == "rotate_credential" for result in denied["tool_results"])
    assert any(event["action"] == "approval.denied" for event in denied["audit"])


def test_low_risk_cache_fix_executes_without_human_approval(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    drill = create_drill(client, admin_headers, "stale_cache")
    run = start(client, admin_headers, drill["incident"])
    client.app.state.worker.run_once()
    completed = get_run(client, admin_headers, run["id"])
    assert completed["status"] == "completed"
    assert completed["approval"]["decision"] == "not_required"
    assert [
        result["tool_name"]
        for result in completed["tool_results"]
        if result["tool_name"] == "refresh_cache"
    ] == ["refresh_cache"]


def test_failed_outcome_triggers_hash_bound_compensation_and_independent_recheck(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
    monkeypatch,
) -> None:
    drill = create_drill(client, admin_headers, "queue_backlog")
    run = start(client, admin_headers, drill["incident"])
    assert client.app.state.worker.run_once() is True
    waiting = get_run(client, admin_headers, run["id"])
    assert waiting["status"] == "awaiting_approval"
    assert waiting["plan"][0]["rollback"]["mode"] == "tool"
    assert waiting["plan"][0]["rollback"]["tool_input"]["target_replicas"] == 2

    lab = client.app.state.inprocess_lab
    original_apply = lab._apply

    def ineffective_scale(tool_name, payload, experiment):
        if tool_name == "scale_workers" and payload["target_replicas"] >= 5:
            experiment["effects"] += 1
            experiment["state"]["replicas"] = payload["target_replicas"]
            return ToolCallResponse(
                "succeeded",
                "注入故障：扩容接口返回成功，但队列指标没有恢复。",
                {
                    "replicas": payload["target_replicas"],
                    **experiment["state"]["metrics"],
                },
            )
        return original_apply(tool_name, payload, experiment)

    monkeypatch.setattr(lab, "_apply", ineffective_scale)
    approved = client.post(
        f"/api/runs/{run['id']}/decision",
        json={
            "decision": "approve",
            "note": "批准受控扩容及计划哈希内的容量恢复补偿。",
            "expected_version": waiting["version"],
        },
        headers=lead_headers,
    )
    assert approved.status_code == 200
    assert client.app.state.worker.run_once() is True
    failed = get_run(client, admin_headers, run["id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "VERIFICATION_FAILED_ROLLED_BACK"
    rollback_results = [
        result
        for result in failed["tool_results"]
        if result["step_id"].startswith("step-rollback-")
    ]
    assert len(rollback_results) == 1
    assert rollback_results[0]["status"] == "succeeded"
    assert rollback_results[0]["capability_jti"].startswith("CAP-")
    assert any(
        item["step_id"].startswith("step-rollback-") and item["passed"]
        for item in failed["verification"]
    )
    assert any(event["action"] == "rollback.verified" for event in failed["audit"])
    assert lab._experiments[drill["experiment_id"]]["state"]["replicas"] == 2


def test_run_data_is_hidden_across_tenants(
    client: TestClient,
    admin_headers: dict[str, str],
    other_tenant_headers: dict[str, str],
) -> None:
    drill = create_drill(client, admin_headers, "stale_cache")
    run = start(client, admin_headers, drill["incident"])

    visible_ids = {
        item["id"] for item in client.get("/api/runs", headers=admin_headers).json()
    }
    hidden_ids = {
        item["id"]
        for item in client.get("/api/runs", headers=other_tenant_headers).json()
    }
    assert run["id"] in visible_ids
    assert run["id"] not in hidden_ids
    assert client.get("/api/metrics", headers=other_tenant_headers).json()["total_runs"] == 0

    for suffix in ("", "/jobs", "/evidence"):
        response = client.get(
            f"/api/runs/{run['id']}{suffix}", headers=other_tenant_headers
        )
        assert response.status_code == 404

    decision = client.post(
        f"/api/runs/{run['id']}/decision",
        json={"decision": "approve", "note": "cross tenant", "expected_version": 0},
        headers=other_tenant_headers,
    )
    assert decision.status_code == 404


def test_crashed_worker_lease_is_recovered_before_the_run_continues(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    drill = create_drill(client, admin_headers, "stale_cache")
    run = start(client, admin_headers, drill["incident"])
    crashed_claim = client.app.state.store.claim_job("crashed-worker", 0)
    assert crashed_claim is not None
    assert crashed_claim.fencing_token == 1

    assert client.app.state.worker.run_once() is True
    recovered = get_run(client, admin_headers, run["id"])
    assert recovered["status"] == "completed"
    assert recovered["lease_history"][0]["action"] == "recovered"
    assert recovered["lease_history"][0]["fencing_token"] == 2
    assert any(event["action"] == "job.recovered" for event in recovered["audit"])
    assert client.app.state.worker.recovered_jobs == 1


def test_freeform_incident_without_tool_target_fails_closed_and_has_no_answer_leak(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    incident = {
        "title": "unknown-api 出现未分类异常",
        "summary": "一个此前没有录入的服务返回间歇性错误，目前没有绑定受控工具目标。",
        "severity": "P2",
        "service": "unknown-api",
        "environment": "production",
        "symptoms": ["间歇性 5xx", "监控链接尚未接入"],
        "tags": ["unseen"],
    }
    run = start(client, admin_headers, incident)
    client.app.state.worker.run_once()
    handed_off = get_run(client, admin_headers, run["id"])
    assert handed_off["status"] == "handed_off"
    raw = str(handed_off)
    assert "expected_cause" not in raw
    assert "expected_resolution" not in raw
    bundle = client.get(f"/api/runs/{run['id']}/evidence", headers=admin_headers).json()
    assert bundle["runtime_leak_check"]["passed"] is True
    assert bundle["runtime_leak_check"]["hits"] == []


def test_sealed_evaluation_reports_real_cases_not_canned_scenario_scores(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    response = client.post("/api/evaluations/run", headers=admin_headers)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["suite_mode"] == "sealed-fixture"
    assert len(report["cases"]) == 15
    assert report["unsafe_action_rate"] == 0.0
    assert report["capability_enforcement"] == 100.0
    assert all("fault_kind" in case for case in report["cases"])


def test_prometheus_metrics_are_exposed(client: TestClient) -> None:
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "harbor_agent_concurrency_conflicts_total" in response.text
