from __future__ import annotations

from typing import Any

import httpx

from ..contracts import ToolCallResponse

class RemoteToolClient:
    name = "remote-http-fault-lab"

    def __init__(self, base_url: str, health_token: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.health_token = health_token
        self.timeout_seconds = timeout_seconds

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
        with httpx.Client(timeout=self.timeout_seconds) as client:
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
            raise RuntimeError(f"tool lab returned non-JSON HTTP {response.status_code}") from exc
        if response.status_code in {401, 403, 404, 409, 422}:
            raise RuntimeError(body.get("detail") or f"tool lab HTTP {response.status_code}")
        if response.status_code >= 500:
            raise RuntimeError(body.get("detail") or f"tool lab HTTP {response.status_code}")
        return ToolCallResponse(
            status=str(body.get("status", "failed")),
            summary=str(body.get("summary", "工具实验室未返回摘要。")),
            output=dict(body.get("output") or {}),
            error=body.get("error"),
        )

    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=1.5) as client:
                response = client.get(
                    f"{self.base_url}/health",
                    headers={"Authorization": f"Bearer {self.health_token}"},
                )
                response.raise_for_status()
                body = response.json()
            return {"status": body.get("status", "ready"), "mode": self.name, **body}
        except Exception as exc:
            return {"status": "unavailable", "mode": self.name, "detail": f"{type(exc).__name__}: lab unreachable"}
