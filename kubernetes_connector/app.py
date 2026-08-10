from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Protocol
from urllib.parse import quote

from fastapi import FastAPI, Header, HTTPException, Request, Response
import httpx
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import ValidationError

from app.security import (
    AuthenticationError,
    AuthorizationError,
    CapabilityService,
    payload_hash,
)
from app.tools import (
    InspectKubernetesWorkloadInput,
    ScaleKubernetesDeploymentInput,
)


TOOL_MODELS = {
    "inspect_kubernetes_workload": InspectKubernetesWorkloadInput,
    "scale_kubernetes_deployment": ScaleKubernetesDeploymentInput,
}
TOOL_CALLS = Counter(
    "harbor_kubernetes_connector_calls_total",
    "Kubernetes connector calls",
    ["tool", "status"],
)
DENIALS = Counter(
    "harbor_kubernetes_connector_denials_total",
    "Kubernetes connector authorization denials",
    ["reason"],
)
ANNOTATION_PREFIX = "agentops.harbor.io"


class KubernetesApiError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class KubernetesApi(Protocol):
    def get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]: ...

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]: ...

    def put(self, path: str, body: dict[str, Any]) -> dict[str, Any]: ...


class InClusterKubernetesApi:
    def __init__(self) -> None:
        host = os.getenv("KUBERNETES_SERVICE_HOST", "")
        port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if not host:
            raise RuntimeError("KUBERNETES_SERVICE_HOST is missing; run this connector in a Pod")
        service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.base_url = f"https://{host}:{port}"
        self.token = (service_account / "token").read_text(encoding="utf-8").strip()
        self.ca_file = str(service_account / "ca.crt")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with httpx.Client(verify=self.ca_file, timeout=httpx.Timeout(8.0, connect=2.0)) as client:
            response = client.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
        if response.status_code >= 400:
            try:
                detail = response.json().get("message", response.text)
            except ValueError:
                detail = response.text
            raise KubernetesApiError(response.status_code, str(detail)[:500])
        try:
            return dict(response.json())
        except ValueError as exc:
            raise KubernetesApiError(502, "Kubernetes API returned non-JSON data") from exc

    def get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body=body)

    def put(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("PUT", path, body=body)


@dataclass(frozen=True)
class ConnectorSettings:
    namespace: str
    allowed_deployments: frozenset[str]
    health_token: str
    capability_secret: str

    @classmethod
    def from_env(cls) -> "ConnectorSettings":
        namespace = os.getenv("KUBERNETES_TARGET_NAMESPACE", "harbor-sandbox").strip()
        allowed = frozenset(
            value.strip()
            for value in os.getenv("KUBERNETES_ALLOWED_DEPLOYMENTS", "demo-api").split(",")
            if value.strip()
        )
        health_token = os.getenv("KUBERNETES_CONNECTOR_HEALTH_TOKEN", "")
        capability_secret = os.getenv("CAPABILITY_SIGNING_SECRET", "")
        if not allowed:
            raise RuntimeError("KUBERNETES_ALLOWED_DEPLOYMENTS cannot be empty")
        if len(health_token) < 24:
            raise RuntimeError("KUBERNETES_CONNECTOR_HEALTH_TOKEN must contain at least 24 characters")
        if len(capability_secret) < 24:
            raise RuntimeError("CAPABILITY_SIGNING_SECRET must contain at least 24 characters")
        return cls(namespace, allowed, health_token, capability_secret)


class NamespaceConnector:
    def __init__(self, settings: ConnectorSettings, api: KubernetesApi) -> None:
        self.settings = settings
        self.api = api
        self.capabilities = CapabilityService(settings.capability_secret)

    @staticmethod
    def _lease_name(job_id: str) -> str:
        return f"harbor-fence-{hashlib.sha256(job_id.encode()).hexdigest()[:32]}"

    def _lease_path(self, job_id: str) -> str:
        namespace = quote(self.settings.namespace, safe="")
        return (
            f"/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases/"
            f"{self._lease_name(job_id)}"
        )

    def _leases_path(self) -> str:
        namespace = quote(self.settings.namespace, safe="")
        return f"/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases"

    def authorize_target(self, namespace: str, deployment: str) -> None:
        if namespace != self.settings.namespace:
            raise HTTPException(status_code=403, detail="namespace is outside the connector scope")
        if deployment not in self.settings.allowed_deployments:
            raise HTTPException(status_code=403, detail="deployment is not in the connector allowlist")

    def acquire_fence(
        self,
        job_id: str,
        fencing_token: int,
        idempotency_key: str,
        current_payload_hash: str,
    ) -> dict[str, Any] | None:
        path = self._lease_path(job_id)
        for _ in range(5):
            try:
                lease = self.api.get(path)
            except KubernetesApiError as exc:
                if exc.status_code != 404:
                    raise
                annotations = {
                    f"{ANNOTATION_PREFIX}/max-fencing-token": str(fencing_token),
                }
                body = {
                    "apiVersion": "coordination.k8s.io/v1",
                    "kind": "Lease",
                    "metadata": {
                        "name": self._lease_name(job_id),
                        "namespace": self.settings.namespace,
                        "annotations": annotations,
                    },
                    "spec": {
                        "holderIdentity": job_id[:128],
                        "leaseDurationSeconds": 300,
                    },
                }
                try:
                    self.api.post(self._leases_path(), body)
                    return None
                except KubernetesApiError as create_error:
                    if create_error.status_code == 409:
                        continue
                    raise

            metadata = lease.setdefault("metadata", {})
            annotations = metadata.setdefault("annotations", {})
            maximum = int(annotations.get(f"{ANNOTATION_PREFIX}/max-fencing-token", "0"))
            if fencing_token < maximum:
                raise HTTPException(
                    status_code=409,
                    detail=f"stale fencing token {fencing_token}; latest is {maximum}",
                )
            prior_key = annotations.get(f"{ANNOTATION_PREFIX}/idempotency-key")
            if prior_key == idempotency_key:
                if annotations.get(f"{ANNOTATION_PREFIX}/payload-hash") != current_payload_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="idempotency key was already used for different input",
                    )
                encoded = annotations.get(f"{ANNOTATION_PREFIX}/response-json")
                if encoded:
                    annotations[f"{ANNOTATION_PREFIX}/max-fencing-token"] = str(
                        max(maximum, fencing_token)
                    )
                    try:
                        self.api.put(path, lease)
                    except KubernetesApiError as update_error:
                        if update_error.status_code == 409:
                            continue
                        raise
                    cached = json.loads(encoded)
                    cached["status"] = "skipped"
                    cached["summary"] = "Kubernetes Lease 幂等命中：返回已提交结果。"
                    return cached
            annotations[f"{ANNOTATION_PREFIX}/max-fencing-token"] = str(
                max(maximum, fencing_token)
            )
            try:
                self.api.put(path, lease)
                return None
            except KubernetesApiError as update_error:
                if update_error.status_code == 409:
                    continue
                raise
        raise KubernetesApiError(409, "could not acquire the Kubernetes fencing Lease")

    def assert_current_fence(self, job_id: str, fencing_token: int) -> None:
        lease = self.api.get(self._lease_path(job_id))
        annotations = lease.get("metadata", {}).get("annotations", {})
        maximum = int(annotations.get(f"{ANNOTATION_PREFIX}/max-fencing-token", "0"))
        if fencing_token != maximum:
            raise HTTPException(
                status_code=409,
                detail=f"fencing token {fencing_token} no longer owns the connector Lease",
            )

    def cache_result(
        self,
        job_id: str,
        fencing_token: int,
        idempotency_key: str,
        current_payload_hash: str,
        response: dict[str, Any],
    ) -> None:
        path = self._lease_path(job_id)
        for _ in range(5):
            lease = self.api.get(path)
            annotations = lease.setdefault("metadata", {}).setdefault("annotations", {})
            maximum = int(annotations.get(f"{ANNOTATION_PREFIX}/max-fencing-token", "0"))
            if fencing_token < maximum:
                raise HTTPException(status_code=409, detail="connector ownership changed before commit")
            annotations[f"{ANNOTATION_PREFIX}/idempotency-key"] = idempotency_key
            annotations[f"{ANNOTATION_PREFIX}/payload-hash"] = current_payload_hash
            annotations[f"{ANNOTATION_PREFIX}/response-json"] = json.dumps(
                response, ensure_ascii=False, separators=(",", ":")
            )
            try:
                self.api.put(path, lease)
                return
            except KubernetesApiError as exc:
                if exc.status_code == 409:
                    continue
                raise
        raise KubernetesApiError(409, "could not persist connector idempotency result")

    def inspect(self, payload: dict[str, Any]) -> dict[str, Any]:
        namespace = payload["namespace"]
        deployment = payload["deployment"]
        target_ready = payload.get("wait_for_ready_replicas")
        deadline = time.monotonic() + int(payload.get("timeout_seconds", 0))
        namespace_path = quote(namespace, safe="")
        deployment_path = quote(deployment, safe="")
        path = f"/apis/apps/v1/namespaces/{namespace_path}/deployments/{deployment_path}"
        timed_out = False
        while True:
            resource = self.api.get(path)
            ready = int(resource.get("status", {}).get("readyReplicas", 0) or 0)
            if target_ready is None or ready >= int(target_ready):
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.5)

        pods = self.api.get(
            f"/api/v1/namespaces/{namespace_path}/pods",
            params={"labelSelector": f"app.kubernetes.io/name={deployment}"},
        )
        event_items: list[dict[str, Any]] = []
        if payload.get("include_events") and int(payload.get("event_limit", 0)) > 0:
            events = self.api.get(
                f"/api/v1/namespaces/{namespace_path}/events",
                params={
                    "fieldSelector": (
                        "involvedObject.kind=Deployment,"
                        f"involvedObject.name={deployment}"
                    )
                },
            )
            for item in list(events.get("items", []))[-int(payload["event_limit"]) :]:
                event_items.append(
                    {
                        "type": item.get("type"),
                        "reason": item.get("reason"),
                        "message": str(item.get("message", ""))[:300],
                    }
                )
        signals = self.api.get(
            f"/api/v1/namespaces/{namespace_path}/configmaps/"
            f"{quote(deployment + '-signals', safe='')}"
        ).get("data", {})

        def number(name: str) -> float:
            try:
                return float(signals.get(name, 0))
            except (TypeError, ValueError):
                return 0.0

        produce = number("produce_per_min")
        per_replica = number("consume_per_replica")
        recommended = (
            min(5, max(1, math.ceil(produce * 1.2 / per_replica)))
            if produce > 0 and per_replica > 0
            else int(resource.get("spec", {}).get("replicas", 0) or 0)
        )
        pod_summaries = []
        for item in pods.get("items", []):
            restarts = sum(
                int(status.get("restartCount", 0) or 0)
                for status in item.get("status", {}).get("containerStatuses", [])
            )
            pod_summaries.append(
                {
                    "name": item.get("metadata", {}).get("name"),
                    "phase": item.get("status", {}).get("phase"),
                    "ready": all(
                        bool(status.get("ready"))
                        for status in item.get("status", {}).get("containerStatuses", [])
                    ),
                    "restarts": restarts,
                }
            )
        status = resource.get("status", {})
        return {
            "provider": "kubernetes-in-cluster-api",
            "source_uri": f"kubernetes://{namespace}/deployments/{deployment}",
            "namespace": namespace,
            "deployment": deployment,
            "resource_version": resource.get("metadata", {}).get("resourceVersion"),
            "replicas": int(resource.get("spec", {}).get("replicas", 0) or 0),
            "ready_replicas": int(status.get("readyReplicas", 0) or 0),
            "available_replicas": int(status.get("availableReplicas", 0) or 0),
            "unavailable_replicas": int(status.get("unavailableReplicas", 0) or 0),
            "queue_depth": number("queue_depth"),
            "produce_per_min": produce,
            "consume_per_replica": per_replica,
            "recommended_replicas": recommended,
            "pods": pod_summaries,
            "warning_events": event_items,
            "ready_wait_timed_out": timed_out,
        }

    def scale(
        self,
        payload: dict[str, Any],
        *,
        job_id: str,
        fencing_token: int,
    ) -> dict[str, Any]:
        namespace = quote(payload["namespace"], safe="")
        deployment = quote(payload["deployment"], safe="")
        path = f"/apis/apps/v1/namespaces/{namespace}/deployments/{deployment}/scale"
        current = self.api.get(path)
        self.assert_current_fence(job_id, fencing_token)
        previous = int(current.get("spec", {}).get("replicas", 0) or 0)
        body = {
            "apiVersion": "autoscaling/v1",
            "kind": "Scale",
            "metadata": {
                "name": payload["deployment"],
                "namespace": payload["namespace"],
                "resourceVersion": current.get("metadata", {}).get("resourceVersion"),
            },
            "spec": {"replicas": payload["target_replicas"]},
        }
        updated = self.api.put(path, body)
        return {
            "provider": "kubernetes-in-cluster-api",
            "source_uri": (
                f"kubernetes://{payload['namespace']}/deployments/"
                f"{payload['deployment']}/scale"
            ),
            "namespace": payload["namespace"],
            "deployment": payload["deployment"],
            "previous_replicas": previous,
            "target_replicas": int(updated.get("spec", {}).get("replicas", payload["target_replicas"])),
            "change_ticket": payload["change_ticket"],
            "resource_version": updated.get("metadata", {}).get("resourceVersion"),
        }


def _bearer(value: str | None) -> str:
    if not value or not value.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token is required")
    return value.removeprefix("Bearer ").strip()


def create_app(
    settings: ConnectorSettings | None = None,
    api: KubernetesApi | None = None,
) -> FastAPI:
    resolved = settings or ConnectorSettings.from_env()
    connector = NamespaceConnector(resolved, api or InClusterKubernetesApi())
    application = FastAPI(title="Harbor Kubernetes Connector", version="3.4.0")
    application.state.connector = connector

    @application.get("/live", include_in_schema=False)
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @application.get("/health")
    def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if _bearer(authorization) != resolved.health_token:
            raise HTTPException(status_code=401, detail="invalid health token")
        first = sorted(resolved.allowed_deployments)[0]
        connector.api.get(
            f"/apis/apps/v1/namespaces/{quote(resolved.namespace, safe='')}/deployments/"
            f"{quote(first, safe='')}"
        )
        return {
            "status": "ready",
            "namespace": resolved.namespace,
            "allowed_deployments": sorted(resolved.allowed_deployments),
            "read_scope": ["deployments", "deployments/scale", "pods", "events", "named-configmap"],
            "write_scope": ["deployments/scale"],
            "forbidden_by_design": ["secrets", "pods/exec", "pod-create", "deployment-template", "rbac"],
        }

    @application.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @application.post("/v1/tools/{tool_name}")
    def invoke(
        tool_name: str,
        raw_payload: dict[str, Any],
        request: Request,
        authorization: str | None = Header(default=None),
        x_idempotency_key: str = Header(...),
        x_harbor_control_tenant: str = Header(...),
        x_harbor_job_id: str = Header(...),
        x_harbor_fencing_token: int = Header(...),
    ) -> dict[str, Any]:
        model = TOOL_MODELS.get(tool_name)
        if model is None:
            raise HTTPException(status_code=404, detail="tool not found")
        if len(x_idempotency_key) != 64:
            raise HTTPException(status_code=422, detail="a 64-character idempotency key is required")
        if not x_harbor_job_id.strip() or len(x_harbor_job_id) > 100:
            raise HTTPException(status_code=422, detail="a valid job id is required")
        if x_harbor_fencing_token < 1:
            raise HTTPException(status_code=422, detail="fencing token must be positive")
        try:
            payload = model.model_validate(raw_payload).model_dump()
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        connector.authorize_target(payload["namespace"], payload["deployment"])
        try:
            connector.capabilities.verify(
                _bearer(authorization),
                tool_name=tool_name,
                payload=payload,
                tenant_id=x_harbor_control_tenant,
                job_id=x_harbor_job_id,
                fencing_token=x_harbor_fencing_token,
            )
        except AuthenticationError as exc:
            DENIALS.labels(reason="authentication").inc()
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except AuthorizationError as exc:
            DENIALS.labels(reason="authorization").inc()
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        current_payload_hash = payload_hash(payload)
        try:
            cached = connector.acquire_fence(
                x_harbor_job_id,
                x_harbor_fencing_token,
                x_idempotency_key,
                current_payload_hash,
            )
            if cached is not None:
                TOOL_CALLS.labels(tool=tool_name, status="skipped").inc()
                return cached
            if tool_name == "inspect_kubernetes_workload":
                output = connector.inspect(payload)
                summary = (
                    f"读取 {payload['namespace']}/{payload['deployment']}："
                    f"ready {output['ready_replicas']}/{output['replicas']}，"
                    f"建议副本 {output['recommended_replicas']}。"
                )
            else:
                output = connector.scale(
                    payload,
                    job_id=x_harbor_job_id,
                    fencing_token=x_harbor_fencing_token,
                )
                summary = (
                    f"已通过 deployments/scale 将 {payload['deployment']} 从 "
                    f"{output['previous_replicas']} 调整到 {output['target_replicas']}。"
                )
            response = {"status": "succeeded", "summary": summary, "output": output, "error": None}
            connector.cache_result(
                x_harbor_job_id,
                x_harbor_fencing_token,
                x_idempotency_key,
                current_payload_hash,
                response,
            )
            TOOL_CALLS.labels(tool=tool_name, status="succeeded").inc()
            return response
        except HTTPException:
            raise
        except KubernetesApiError as exc:
            if exc.status_code in {401, 403, 404, 409, 422}:
                raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
            raise HTTPException(status_code=502, detail=f"Kubernetes API failure: {exc.detail}") from exc

    return application
