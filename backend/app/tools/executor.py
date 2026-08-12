from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable

from pydantic import ValidationError

from ..schemas import PlanStep, ToolResult, UserIdentity
from ..store import LeaseLostError
from ..store.idempotency import tool_idempotency_key
from .capability import CapabilityService
from .contracts import ToolClient
from .registry import TOOL_REGISTRY

class ToolExecutor:
    def __init__(self, client: ToolClient, capabilities: CapabilityService) -> None:
        self.client = client
        self.capabilities = capabilities

    def execute(
        self,
        step: PlanStep,
        *,
        run_id: str,
        plan_hash_value: str,
        actor: UserIdentity,
        attempt: int,
        previous_results: list[ToolResult],
        job_id: str,
        fencing_token: int,
        lease_guard: Callable[[], None],
    ) -> ToolResult:
        lease_guard()
        spec = TOOL_REGISTRY.get(step.tool_name)
        if spec is None:
            return self._failure(step, run_id, attempt, "工具未注册，已拒绝执行")
        try:
            validated = spec.input_model.model_validate(step.tool_input).model_dump()
        except ValidationError as exc:
            return self._failure(step, run_id, attempt, f"参数校验失败：{exc.errors()[0]['msg']}")

        idempotency_key = self._idempotency_key(run_id, plan_hash_value, step.id, validated)
        prior_success = next(
            (
                result
                for result in reversed(previous_results)
                if result.idempotency_key == idempotency_key and result.status in {"succeeded", "skipped"}
            ),
            None,
        )
        if prior_success:
            return ToolResult(
                step_id=step.id,
                tool_name=step.tool_name,
                status="skipped",
                summary="运行记录幂等命中：该动作不重复执行。",
                output=prior_success.output,
                idempotency_key=idempotency_key,
                duration_ms=0,
                attempt=attempt,
                transport=self.client.name,
                capability_jti=prior_success.capability_jti,
                job_id=job_id,
                fencing_token=fencing_token,
            )

        lease_guard()
        try:
            grant = self.capabilities.issue(
                run_id=run_id,
                plan_hash_value=plan_hash_value,
                step_id=step.id,
                tool_name=step.tool_name,
                payload=validated,
                actor=actor,
                required_role=spec.required_role,
                job_id=job_id,
                fencing_token=fencing_token,
            )
        except Exception as exc:
            return self._failure(step, run_id, attempt, f"能力授权失败：{exc}")

        started = time.perf_counter()
        lease_guard()
        try:
            response = self.client.invoke(
                step.tool_name,
                validated,
                idempotency_key,
                grant.token,
                actor.tenant_id,
                job_id,
                fencing_token,
            )
        except LeaseLostError:
            raise
        except Exception as exc:
            return ToolResult(
                step_id=step.id,
                tool_name=step.tool_name,
                status="unknown",
                summary="工具调用响应未知；禁止使用新幂等键重试，已停止后续写操作。",
                output={"retryable_with_same_key": True, "code": "tool_result_unknown"},
                idempotency_key=idempotency_key,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {str(exc)[:240]}",
                attempt=attempt,
                transport=self.client.name,
                capability_jti=grant.jti,
                job_id=job_id,
                fencing_token=fencing_token,
            )
        lease_guard()
        try:
            status = response.status if response.status in {"succeeded", "failed", "skipped", "unknown"} else "failed"
            return ToolResult(
                step_id=step.id,
                tool_name=step.tool_name,
                status=status,
                summary=response.summary,
                output=response.output,
                idempotency_key=idempotency_key,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error=response.error,
                attempt=attempt,
                transport=self.client.name,
                capability_jti=grant.jti,
                job_id=job_id,
                fencing_token=fencing_token,
            )
        except LeaseLostError:
            raise

    @staticmethod
    def _idempotency_key(
        run_id: str, plan_hash_value: str, step_id: str, payload: dict[str, Any]
    ) -> str:
        return tool_idempotency_key(run_id, plan_hash_value, step_id, payload)

    def _failure(self, step: PlanStep, run_id: str, attempt: int, message: str) -> ToolResult:
        return ToolResult(
            step_id=step.id,
            tool_name=step.tool_name,
            status="failed",
            summary=message,
            idempotency_key=hashlib.sha256(f"{run_id}:{step.id}:{json.dumps(step.tool_input, sort_keys=True)}".encode()).hexdigest(),
            error=message,
            attempt=attempt,
            transport=self.client.name,
        )
