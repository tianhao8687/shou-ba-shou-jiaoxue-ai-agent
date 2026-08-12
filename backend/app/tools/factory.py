from __future__ import annotations

from .builtin import (
    InMemoryFaultLabClient,
    KubernetesConnectorClient,
    PrometheusReadOnlyClient,
    RemoteToolClient,
)
from .capability import CapabilityService
from .contracts import ToolClient
from .executor import ToolExecutor
from .routing import RoutingToolClient

def create_tool_executor(
    *,
    mode: str,
    base_url: str,
    health_token: str,
    timeout_seconds: float,
    capabilities: CapabilityService,
    prometheus_url: str = "",
    prometheus_bearer_token: str = "",
    prometheus_timeout_seconds: float = 5.0,
    kubernetes_connector_url: str = "",
    kubernetes_connector_health_token: str = "",
    kubernetes_connector_timeout_seconds: float = 10.0,
) -> tuple[ToolExecutor, InMemoryFaultLabClient | None]:
    prometheus = (
        PrometheusReadOnlyClient(
            prometheus_url,
            prometheus_bearer_token,
            prometheus_timeout_seconds,
            capabilities,
        )
        if prometheus_url
        else None
    )
    kubernetes = (
        KubernetesConnectorClient(
            kubernetes_connector_url,
            kubernetes_connector_health_token,
            kubernetes_connector_timeout_seconds,
        )
        if kubernetes_connector_url
        else None
    )
    if mode == "remote":
        client: ToolClient = RemoteToolClient(base_url, health_token, timeout_seconds)
        if prometheus is not None or kubernetes is not None:
            client = RoutingToolClient(client, prometheus, kubernetes)
        return ToolExecutor(client, capabilities), None
    if mode != "inprocess":
        raise ValueError("TOOL_MODE must be 'remote' or 'inprocess'")
    lab = InMemoryFaultLabClient()
    client = (
        RoutingToolClient(lab, prometheus, kubernetes)
        if prometheus is not None or kubernetes is not None
        else lab
    )
    return ToolExecutor(client, capabilities), lab
