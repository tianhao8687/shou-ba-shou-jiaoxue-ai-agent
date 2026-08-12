from __future__ import annotations

import math
from typing import Any

import httpx

from ...security import CapabilityService
from ..contracts import PrometheusServiceInput, ToolCallResponse

class PrometheusReadOnlyClient:
    """Bounded, template-only Prometheus adapter for real read-only observations."""

    name = "prometheus-http-read-only"
    tool_names = frozenset({"query_prometheus_slo"})

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        timeout_seconds: float,
        capabilities: CapabilityService,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("Prometheus URL must be an absolute http(s) URL")
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        self.capabilities = capabilities
        self.transport = transport

    @staticmethod
    def _queries(service: str, environment: str, window_minutes: int) -> dict[str, str]:
        selector = f'service="{service}",environment="{environment}"'
        window = f"{window_minutes}m"
        total = f'sum(rate(http_requests_total{{{selector}}}[{window}]))'
        errors = (
            f'sum(rate(http_requests_total{{{selector},status=~"5.."}}[{window}]))'
        )
        return {
            "request_rate_rps": total,
            "error_rate_percent": f"100 * ({errors}) / clamp_min(({total}), 0.000000001)",
            "p95_ms": (
                "1000 * histogram_quantile(0.95, sum by (le) "
                f'(rate(http_request_duration_seconds_bucket{{{selector}}}[{window}])))'
            ),
            "up": f'min(up{{{selector}}})',
        }

    @staticmethod
    def _scalar(body: dict[str, Any]) -> float | None:
        if body.get("status") != "success":
            raise RuntimeError("Prometheus query did not return success")
        data = body.get("data") or {}
        result = data.get("result") or []
        if not result:
            return None
        raw = (result[0].get("value") or [None, None])[-1]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

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
        del idempotency_key  # This endpoint is strictly read-only.
        if tool_name not in self.tool_names:
            raise RuntimeError(f"Prometheus adapter does not implement {tool_name}")
        validated = PrometheusServiceInput.model_validate(payload).model_dump()
        self.capabilities.verify(
            capability_token,
            tool_name=tool_name,
            payload=validated,
            tenant_id=tenant_id,
            job_id=job_id,
            fencing_token=fencing_token,
        )
        headers = (
            {"Authorization": f"Bearer {self.bearer_token}"}
            if self.bearer_token
            else {}
        )
        values: dict[str, float | None] = {}
        with httpx.Client(
            timeout=httpx.Timeout(self.timeout_seconds, connect=2.0),
            transport=self.transport,
        ) as client:
            for field, query in self._queries(
                validated["service"],
                validated["environment"],
                validated["window_minutes"],
            ).items():
                response = client.get(
                    f"{self.base_url}/api/v1/query",
                    params={"query": query},
                    headers=headers,
                )
                response.raise_for_status()
                values[field] = self._scalar(response.json())
        sample_count = sum(value is not None for value in values.values())
        return ToolCallResponse(
            "succeeded",
            (
                f"Prometheus 返回 {sample_count}/4 个可用 SLO 指标。"
                if sample_count
                else "Prometheus 查询成功，但目标标签没有匹配时序数据。"
            ),
            {
                "provider": self.name,
                "source_uri": f"{self.base_url}/api/v1/query",
                "service": validated["service"],
                "environment": validated["environment"],
                "window_minutes": validated["window_minutes"],
                "sample_count": sample_count,
                **values,
            },
        )

    def health(self) -> dict[str, Any]:
        try:
            headers = (
                {"Authorization": f"Bearer {self.bearer_token}"}
                if self.bearer_token
                else {}
            )
            with httpx.Client(timeout=1.5, transport=self.transport) as client:
                response = client.get(f"{self.base_url}/-/ready", headers=headers)
                response.raise_for_status()
            return {
                "status": "ready",
                "mode": self.name,
                "query_policy": "server-side-templates-only",
                "write_capability": False,
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "mode": self.name,
                "detail": f"{type(exc).__name__}: Prometheus is not ready",
            }
