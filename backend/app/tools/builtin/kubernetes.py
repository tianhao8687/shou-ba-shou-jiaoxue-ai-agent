from __future__ import annotations

from typing import Any

import httpx

from ..contracts import ToolCallResponse

class KubernetesConnectorClient:
    """HTTP boundary to the in-cluster, namespace-scoped Kubernetes connector."""

    name = "kubernetes-namespace-connector"
    tool_names = frozenset(
        {"inspect_kubernetes_workload", "scale_kubernetes_deployment"}
    )

    def __init__(
        self,
        base_url: str,
        health_token: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("Kubernetes connector URL must be an absolute http(s) URL")
        self.health_token = health_token
        self.timeout_seconds = timeout_seconds
        self.transport = transport

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
        if tool_name not in self.tool_names:
            raise RuntimeError(f"Kubernetes connector does not implement {tool_name}")
        with httpx.Client(
            timeout=httpx.Timeout(self.timeout_seconds, connect=2.0),
            transport=self.transport,
        ) as client:
            response = client.post(
                f"{self.base_url}/v1/tools/{tool_name}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {capability_token}",
                    "X-Idempotency-Key": idempotency_key,
                    "X-Harbor-Control-Tenant": tenant_id,
                    "X-Harbor-Job-Id": job_id,
                    "X-Harbor-Fencing-Token": str(fencing_token),
                },
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Kubernetes connector returned non-JSON HTTP {response.status_code}"
            ) from exc
        if response.status_code >= 400:
            raise RuntimeError(
                body.get("detail") or f"Kubernetes connector HTTP {response.status_code}"
            )
        return ToolCallResponse(
            status=str(body.get("status", "failed")),
            summary=str(body.get("summary", "Kubernetes connector 未返回摘要。")),
            output=dict(body.get("output") or {}),
            error=body.get("error"),
        )

    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=2.0, transport=self.transport) as client:
                response = client.get(
                    f"{self.base_url}/health",
                    headers={"Authorization": f"Bearer {self.health_token}"},
                )
                response.raise_for_status()
                body = response.json()
            return {"status": body.get("status", "ready"), "mode": self.name, **body}
        except Exception as exc:
            return {
                "status": "unavailable",
                "mode": self.name,
                "detail": f"{type(exc).__name__}: Kubernetes connector is not ready",
            }
