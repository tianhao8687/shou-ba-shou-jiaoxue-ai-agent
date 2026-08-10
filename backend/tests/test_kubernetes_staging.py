from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.tools import RoutingToolClient, ToolCallResponse


class FakeKubernetesConnector:
    name = "fake-kubernetes-namespace-connector"
    tool_names = frozenset(
        {"inspect_kubernetes_workload", "scale_kubernetes_deployment"}
    )

    def __init__(self, *, fail_readiness: bool = False) -> None:
        self.replicas = 1
        self.ready_replicas = 1
        self.fail_readiness = fail_readiness
        self.scale_targets: list[int] = []

    def invoke(
        self,
        tool_name: str,
        payload: dict[str, Any],
        idempotency_key: str,
        capability_token: str,
        tenant_id: str,
        job_id: str,
        fencing_token: int,
    ) -> ToolCallResponse:
        del idempotency_key, capability_token, tenant_id, job_id, fencing_token
        if tool_name == "scale_kubernetes_deployment":
            previous = self.replicas
            self.replicas = int(payload["target_replicas"])
            self.scale_targets.append(self.replicas)
            self.ready_replicas = (
                1 if self.fail_readiness and self.replicas > 1 else self.replicas
            )
            return ToolCallResponse(
                "succeeded",
                "scale subresource updated",
                {
                    "previous_replicas": previous,
                    "target_replicas": self.replicas,
                    "source_uri": "kubernetes://harbor-sandbox/deployments/demo-api/scale",
                },
            )
        return ToolCallResponse(
            "succeeded",
            "workload inspected",
            {
                "provider": self.name,
                "source_uri": "kubernetes://harbor-sandbox/deployments/demo-api",
                "namespace": "harbor-sandbox",
                "deployment": "demo-api",
                "replicas": self.replicas,
                "ready_replicas": self.ready_replicas,
                "available_replicas": self.ready_replicas,
                "unavailable_replicas": self.replicas - self.ready_replicas,
                "queue_depth": 1500,
                "produce_per_min": 600,
                "consume_per_replica": 200,
                "recommended_replicas": 4,
                "pods": [],
                "warning_events": [],
                "ready_wait_timed_out": self.ready_replicas < self.replicas,
            },
        )

    def health(self) -> dict[str, str]:
        return {"status": "ready", "mode": self.name}


def install_connector(client: TestClient, connector: FakeKubernetesConnector) -> None:
    lab = client.app.state.tool_executor.client
    client.app.state.tool_executor.client = RoutingToolClient(
        lab, kubernetes=connector
    )
    client.app.state.engine.kubernetes_connector_enabled = True
    client.app.state.engine.kubernetes_staging_namespace = "harbor-sandbox"


def start_staging_run(client: TestClient, admin_headers: dict[str, str]) -> dict:
    response = client.post(
        "/api/runs",
        headers=admin_headers,
        json={
            "incident": {
                "title": "demo-api staging backlog",
                "summary": "预发布环境积压持续升高，需要读取真实 Kubernetes 工作负载和容量信号。",
                "severity": "P2",
                "service": "demo-api",
                "environment": "staging",
                "symptoms": ["queue depth continues rising"],
            }
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def approve(
    client: TestClient,
    run: dict,
    lead_headers: dict[str, str],
) -> dict:
    response = client.post(
        f"/api/runs/{run['id']}/decision",
        headers=lead_headers,
        json={
            "decision": "approve",
            "note": "approved namespace-scoped scale after evidence review",
            "expected_version": run["version"],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_staging_kubernetes_scale_requires_approval_and_is_independently_verified(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
) -> None:
    connector = FakeKubernetesConnector()
    install_connector(client, connector)
    created = start_staging_run(client, admin_headers)
    assert client.app.state.worker.run_once() is True
    waiting = client.get(f"/api/runs/{created['id']}", headers=admin_headers).json()
    assert waiting["status"] == "awaiting_approval"
    assert waiting["risk_level"] == "medium"
    assert waiting["plan"][0]["tool_name"] == "scale_kubernetes_deployment"
    assert connector.scale_targets == []

    queued = approve(client, waiting, lead_headers)
    assert queued["status"] == "queued"
    assert client.app.state.worker.run_once() is True
    completed = client.get(f"/api/runs/{created['id']}", headers=admin_headers).json()
    assert completed["status"] == "completed"
    assert connector.scale_targets == [4]
    assert completed["verification"][0]["passed"] is True
    assert any(
        result["tool_name"] == "inspect_kubernetes_workload"
        and result["step_id"].startswith("step-verify-")
        for result in completed["tool_results"]
    )


def test_staging_kubernetes_failed_readiness_rolls_back_to_observed_replicas(
    client: TestClient,
    admin_headers: dict[str, str],
    lead_headers: dict[str, str],
) -> None:
    connector = FakeKubernetesConnector(fail_readiness=True)
    install_connector(client, connector)
    created = start_staging_run(client, admin_headers)
    assert client.app.state.worker.run_once() is True
    waiting = client.get(f"/api/runs/{created['id']}", headers=admin_headers).json()
    approve(client, waiting, lead_headers)
    assert client.app.state.worker.run_once() is True

    failed = client.get(f"/api/runs/{created['id']}", headers=admin_headers).json()
    assert failed["status"] == "failed"
    assert failed["error_code"] == "VERIFICATION_FAILED_ROLLED_BACK"
    assert connector.scale_targets == [4, 1]
    assert connector.replicas == 1
    assert any(
        event["action"] == "rollback.verified" for event in failed["audit"]
    )
