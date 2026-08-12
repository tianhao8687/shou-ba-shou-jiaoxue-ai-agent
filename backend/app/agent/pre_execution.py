from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from ..schemas import Check, Observation, PlanStep, RunRecord, ToolResult, utc_now
from ..tools import TOOL_REGISTRY, ToolExecutor
from .context import LeaseContext, SYSTEM_AGENT, field_value


class PreExecutionObservationError(RuntimeError):
    """A fresh read could not produce an authoritative state snapshot."""


class PreExecutionObservationInvalid(PreExecutionObservationError):
    """A read returned data that cannot safely drive precondition evaluation."""


@dataclass(frozen=True)
class FreshObservation:
    data: dict[str, Any]
    observations: tuple[Observation, ...]
    results: tuple[ToolResult, ...]
    source: str
    observed_at: datetime
    target: dict[str, Any]

    @property
    def idempotency_key(self) -> str:
        return self.results[-1].idempotency_key if self.results else "not-issued"


def _contains_field(context: dict[str, Any], path: str) -> bool:
    value: Any = context
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return True


def _value_has_expected_shape(check: Check, actual: Any) -> bool:
    expected = check.value
    if check.operator in {"lt", "lte", "gt", "gte"}:
        return (
            isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and isinstance(expected, (int, float))
            and not isinstance(expected, bool)
        )
    if check.operator in {"eq", "ne"} and isinstance(
        expected, (str, int, float, bool)
    ):
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            return isinstance(actual, (int, float)) and not isinstance(actual, bool)
        return isinstance(actual, type(expected))
    return actual is not None


def _target(step: PlanStep) -> dict[str, Any]:
    allowed = (
        "experiment_id",
        "service",
        "namespace",
        "deployment",
        "instance",
        "tenant",
        "category",
        "credential_id",
        "partition",
    )
    return {key: step.tool_input[key] for key in allowed if key in step.tool_input}


class PreExecutionObserver:
    """Runs tool-owned read contracts without rerunning the whole Agent workflow."""

    def __init__(self, tool_executor: ToolExecutor) -> None:
        self.tool_executor = tool_executor

    def observe(
        self,
        *,
        record: RunRecord,
        step: PlanStep,
        lease: LeaseContext,
        lease_guard,
    ) -> FreshObservation:
        spec = TOOL_REGISTRY.get(step.tool_name)
        if spec is None or spec.read_only:
            raise PreExecutionObservationInvalid(
                f"tool {step.tool_name} does not have a write observation contract"
            )
        if not record.policy.plan_hash:
            raise PreExecutionObservationInvalid("plan hash is missing")

        actor = SYSTEM_AGENT.model_copy(update={"tenant_id": record.tenant_id})
        combined: dict[str, Any] = {}
        observations: list[Observation] = []
        results: list[ToolResult] = []
        sources: list[str] = []
        required_fields: set[str] = set()
        for contract in spec.pre_execution_observations:
            try:
                payload = contract.payload_builder(record, step)
            except Exception as exc:
                raise PreExecutionObservationInvalid(
                    f"cannot build fresh observation payload: {exc}"
                ) from exc
            observation_id = f"step-preobserve-{uuid4().hex[:12]}"
            result = self.tool_executor.observe_before_execution(
                tool_name=contract.tool_name,
                payload=payload,
                observation_id=observation_id,
                guarded_step_id=step.id,
                run_id=record.id,
                plan_hash_value=record.policy.plan_hash,
                actor=actor,
                attempt=record.attempt,
                job_id=lease.job_id,
                fencing_token=lease.fencing_token,
                lease_guard=lease_guard,
            )
            results.append(result)
            if result.status not in {"succeeded", "skipped"}:
                raise PreExecutionObservationError(
                    result.error or result.summary
                )
            if not result.output:
                raise PreExecutionObservationInvalid(
                    f"fresh observation {contract.tool_name} returned an empty object"
                )
            observed_at = utc_now()
            source_uri = result.output.get("source_uri")
            source = (
                str(source_uri)
                if isinstance(source_uri, str) and source_uri
                else result.transport
            )
            sources.append(source)
            combined.update(result.output)
            observations.append(
                Observation(
                    id=f"PREOBS-{uuid4().hex[:12].upper()}",
                    step_id=step.id,
                    tool_name=contract.tool_name,
                    summary=f"执行前最新观测：{result.summary}",
                    data=result.output,
                    captured_at=observed_at,
                    transport=result.transport,
                    source_uri=source_uri if isinstance(source_uri, str) else None,
                )
            )
            required_fields.update(contract.required_fields)

        required_fields.update(
            check.field
            for check in step.preconditions
            if check.field != "__result_status__"
        )
        missing = sorted(
            field for field in required_fields if not _contains_field(combined, field)
        )
        if missing:
            raise PreExecutionObservationInvalid(
                f"fresh observation is missing required fields: {missing}"
            )
        invalid = sorted(
            check.field
            for check in step.preconditions
            if check.field != "__result_status__"
            and not _value_has_expected_shape(check, field_value(combined, check.field))
        )
        if invalid:
            raise PreExecutionObservationInvalid(
                f"fresh observation has invalid field types: {invalid}"
            )
        return FreshObservation(
            data=combined,
            observations=tuple(observations),
            results=tuple(results),
            source=",".join(dict.fromkeys(sources)),
            observed_at=max(item.captured_at for item in observations),
            target=_target(step),
        )
