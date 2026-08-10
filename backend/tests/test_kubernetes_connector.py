from __future__ import annotations

from copy import deepcopy

from fastapi.testclient import TestClient

from app.schemas import UserIdentity
from app.security import CapabilityService
from app.tools import (
    InspectKubernetesWorkloadInput,
    ScaleKubernetesDeploymentInput,
)
from kubernetes_connector.app import (
    ConnectorSettings,
    KubernetesApiError,
    create_app,
)


SECRET = "test-kubernetes-capability-secret"
HEALTH_TOKEN = "test-kubernetes-health-token"


class FakeKubernetesApi:
    def __init__(self) -> None:
        self.replicas = 1
        self.ready_replicas = 1
        self.resource_version = 1
        self.leases: dict[str, dict] = {}

    def _deployment(self) -> dict:
        return {
            "metadata": {"name": "demo-api", "resourceVersion": str(self.resource_version)},
            "spec": {"replicas": self.replicas},
            "status": {
                "readyReplicas": self.ready_replicas,
                "availableReplicas": self.ready_replicas,
                "unavailableReplicas": self.replicas - self.ready_replicas,
            },
        }

    def get(self, path: str, *, params=None) -> dict:
        del params
        if "/leases/" in path:
            if path not in self.leases:
                raise KubernetesApiError(404, "lease not found")
            return deepcopy(self.leases[path])
        if path.endswith("/deployments/demo-api/scale"):
            return {
                "metadata": {
                    "name": "demo-api",
                    "namespace": "harbor-sandbox",
                    "resourceVersion": str(self.resource_version),
                },
                "spec": {"replicas": self.replicas},
            }
        if path.endswith("/deployments/demo-api"):
            return self._deployment()
        if path.endswith("/pods"):
            return {
                "items": [
                    {
                        "metadata": {"name": f"demo-api-{index}"},
                        "status": {
                            "phase": "Running",
                            "containerStatuses": [
                                {"ready": index <= self.ready_replicas, "restartCount": 0}
                            ],
                        },
                    }
                    for index in range(1, self.replicas + 1)
                ]
            }
        if path.endswith("/events"):
            return {"items": []}
        if path.endswith("/configmaps/demo-api-signals"):
            return {
                "data": {
                    "queue_depth": "1500",
                    "produce_per_min": "600",
                    "consume_per_replica": "200",
                }
            }
        raise KubernetesApiError(404, f"unexpected path {path}")

    def post(self, path: str, body: dict) -> dict:
        lease_path = f"{path}/{body['metadata']['name']}"
        if lease_path in self.leases:
            raise KubernetesApiError(409, "lease already exists")
        stored = deepcopy(body)
        stored["metadata"]["resourceVersion"] = "1"
        self.leases[lease_path] = stored
        return deepcopy(stored)

    def put(self, path: str, body: dict) -> dict:
        if "/leases/" in path:
            current = self.leases.get(path)
            if current is None:
                raise KubernetesApiError(404, "lease not found")
            if body["metadata"].get("resourceVersion") != current["metadata"].get(
                "resourceVersion"
            ):
                raise KubernetesApiError(409, "lease resource version conflict")
            stored = deepcopy(body)
            stored["metadata"]["resourceVersion"] = str(
                int(current["metadata"]["resourceVersion"]) + 1
            )
            self.leases[path] = stored
            return deepcopy(stored)
        if path.endswith("/deployments/demo-api/scale"):
            if body["metadata"].get("resourceVersion") != str(self.resource_version):
                raise KubernetesApiError(409, "scale resource version conflict")
            self.replicas = int(body["spec"]["replicas"])
            self.ready_replicas = self.replicas
            self.resource_version += 1
            return {
                "metadata": {"resourceVersion": str(self.resource_version)},
                "spec": {"replicas": self.replicas},
            }
        raise KubernetesApiError(404, f"unexpected path {path}")


def connector_client() -> tuple[TestClient, FakeKubernetesApi, CapabilityService]:
    api = FakeKubernetesApi()
    settings = ConnectorSettings(
        namespace="harbor-sandbox",
        allowed_deployments=frozenset({"demo-api"}),
        health_token=HEALTH_TOKEN,
        capability_secret=SECRET,
    )
    return TestClient(create_app(settings, api)), api, CapabilityService(SECRET)


def headers(
    capabilities: CapabilityService,
    tool_name: str,
    payload: dict,
    *,
    job_id: str,
    fencing_token: int,
    idempotency_key: str,
) -> dict[str, str]:
    role = "on-call-lead" if tool_name == "scale_kubernetes_deployment" else "observer"
    actor = UserIdentity(
        username="lead@harbor.local",
        display_name="Lead",
        roles=[role],
        tenant_id="xm-ops",
    )
    grant = capabilities.issue(
        run_id="RUN-KUBE",
        plan_hash_value="f" * 64,
        step_id=f"step-{tool_name}",
        tool_name=tool_name,
        payload=payload,
        actor=actor,
        required_role=role,
        job_id=job_id,
        fencing_token=fencing_token,
    )
    return {
        "Authorization": f"Bearer {grant.token}",
        "X-Idempotency-Key": idempotency_key,
        "X-Harbor-Control-Tenant": "xm-ops",
        "X-Harbor-Job-Id": job_id,
        "X-Harbor-Fencing-Token": str(fencing_token),
    }


def test_connector_reads_only_allowlisted_namespace_and_rejects_injected_fields() -> None:
    client, _, capabilities = connector_client()
    payload = InspectKubernetesWorkloadInput(
        namespace="harbor-sandbox", deployment="demo-api"
    ).model_dump()
    response = client.post(
        "/v1/tools/inspect_kubernetes_workload",
        json=payload,
        headers=headers(
            capabilities,
            "inspect_kubernetes_workload",
            payload,
            job_id="job-read",
            fencing_token=1,
            idempotency_key="a" * 64,
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["output"]["recommended_replicas"] == 4
    assert response.json()["output"]["source_uri"] == (
        "kubernetes://harbor-sandbox/deployments/demo-api"
    )

    escaped = {**payload, "namespace": "kube-system"}
    denied = client.post(
        "/v1/tools/inspect_kubernetes_workload",
        json=escaped,
        headers=headers(
            capabilities,
            "inspect_kubernetes_workload",
            escaped,
            job_id="job-escape",
            fencing_token=1,
            idempotency_key="b" * 64,
        ),
    )
    assert denied.status_code == 403

    injected = client.post(
        "/v1/tools/inspect_kubernetes_workload",
        json={**payload, "resource": "secrets"},
        headers=headers(
            capabilities,
            "inspect_kubernetes_workload",
            payload,
            job_id="job-injected",
            fencing_token=1,
            idempotency_key="c" * 64,
        ),
    )
    assert injected.status_code == 422


def test_scale_is_cas_bound_idempotent_and_rejects_stale_fencing_token() -> None:
    client, api, capabilities = connector_client()
    payload = ScaleKubernetesDeploymentInput(
        namespace="harbor-sandbox",
        deployment="demo-api",
        target_replicas=4,
        change_ticket="CHG-4001",
    ).model_dump()
    first_headers = headers(
        capabilities,
        "scale_kubernetes_deployment",
        payload,
        job_id="job-scale",
        fencing_token=1,
        idempotency_key="d" * 64,
    )
    first = client.post(
        "/v1/tools/scale_kubernetes_deployment", json=payload, headers=first_headers
    )
    assert first.status_code == 200, first.text
    assert api.replicas == 4
    replay = client.post(
        "/v1/tools/scale_kubernetes_deployment", json=payload, headers=first_headers
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "skipped"
    assert api.replicas == 4

    newer_payload = {**payload, "target_replicas": 3}
    newer = client.post(
        "/v1/tools/scale_kubernetes_deployment",
        json=newer_payload,
        headers=headers(
            capabilities,
            "scale_kubernetes_deployment",
            newer_payload,
            job_id="job-scale",
            fencing_token=2,
            idempotency_key="e" * 64,
        ),
    )
    assert newer.status_code == 200, newer.text
    assert api.replicas == 3

    stale_payload = {**payload, "target_replicas": 2}
    stale = client.post(
        "/v1/tools/scale_kubernetes_deployment",
        json=stale_payload,
        headers=headers(
            capabilities,
            "scale_kubernetes_deployment",
            stale_payload,
            job_id="job-scale",
            fencing_token=1,
            idempotency_key="f" * 64,
        ),
    )
    assert stale.status_code == 409
    assert "stale fencing token" in stale.json()["detail"]
    assert api.replicas == 3


def test_health_reports_narrow_write_scope() -> None:
    client, _, _ = connector_client()
    assert client.get("/health").status_code == 401
    response = client.get(
        "/health", headers={"Authorization": f"Bearer {HEALTH_TOKEN}"}
    )
    assert response.status_code == 200
    assert response.json()["write_scope"] == ["deployments/scale"]
    assert "secrets" in response.json()["forbidden_by_design"]
