from __future__ import annotations

from typing import Any

from .builtin.prometheus import PrometheusReadOnlyClient
from .contracts import ToolCallResponse, ToolClient

class RoutingToolClient:
    """Route each tool to its separately permissioned external boundary."""

    name = "routed-tool-boundary"

    def __init__(
        self,
        lab: ToolClient,
        prometheus: PrometheusReadOnlyClient | None = None,
        kubernetes: ToolClient | None = None,
    ) -> None:
        self.lab = lab
        self.prometheus = prometheus
        self.kubernetes = kubernetes

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
        if self.prometheus is not None and tool_name in self.prometheus.tool_names:
            target = self.prometheus
        elif (
            self.kubernetes is not None
            and tool_name in getattr(self.kubernetes, "tool_names", frozenset())
        ):
            target = self.kubernetes
        else:
            target = self.lab
        return target.invoke(
            tool_name,
            payload,
            idempotency_key,
            capability_token,
            tenant_id,
            job_id,
            fencing_token,
        )

    def health(self) -> dict[str, Any]:
        lab_health = self.lab.health()
        boundaries: dict[str, dict[str, Any]] = {"lab": lab_health}
        if self.prometheus is not None:
            boundaries["production_observer"] = self.prometheus.health()
        if self.kubernetes is not None:
            boundaries["kubernetes_staging"] = self.kubernetes.health()
        ready = all(item.get("status") == "ready" for item in boundaries.values())
        return {
            "status": "ready" if ready else "unavailable",
            "mode": self.name,
            **boundaries,
        }
