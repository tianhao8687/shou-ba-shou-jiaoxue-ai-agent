from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .context import EnginePort, LeaseContext, field_value
from .pre_execution import PreExecutionObservationError
from ..schemas import (
    Check,
    CompensationRecord,
    PlanStep,
    RollbackPlan,
    RunRecord,
    UserIdentity,
    VerificationResult,
    utc_now,
)
from ..tools import TOOL_REGISTRY


class CompensationPreparationError(RuntimeError):
    """A write cannot proceed because its deterministic inverse is invalid."""


@dataclass(frozen=True)
class CompensationOutcome:
    restored: bool
    manual_intervention_required: bool
    detail: str


def _compensation_step(
    original: PlanStep,
    compensation: CompensationRecord,
) -> PlanStep:
    spec = TOOL_REGISTRY[compensation.tool_name]
    return PlanStep(
        id=compensation.compensation_step_id,
        title="执行 Saga 补偿",
        objective="将已明确成功的写操作恢复到执行前最新观测到的状态。",
        tool_name=compensation.tool_name,
        tool_input=compensation.tool_input,
        evidence_ids=original.evidence_ids,
        success_criteria=[
            Check(
                field="__result_status__",
                operator="eq",
                value="succeeded",
                description="补偿工具必须返回明确成功状态",
            )
        ],
        rollback=RollbackPlan(
            mode="manual",
            rationale="补偿失败或结果未知时禁止递归补偿，必须由值班负责人接管。",
        ),
        risk=spec.risk,
        rationale="补偿参数由工具合同根据写入前真实状态生成，不由模型推测。",
    )


def prepare_compensation(
    engine: EnginePort,
    record: RunRecord,
    step: PlanStep,
    before_state: dict[str, Any],
    lease: LeaseContext,
) -> CompensationRecord | None:
    """Persist an inverse before the side effect so crash recovery has the baseline."""

    spec = TOOL_REGISTRY[step.tool_name]
    if not spec.compensatable:
        return None
    existing = next(
        (
            item
            for item in record.compensations
            if item.original_step_id == step.id
        ),
        None,
    )
    if existing is not None:
        return existing
    if spec.compensation_builder is None or not record.policy.plan_hash:
        raise CompensationPreparationError(
            f"tool {step.tool_name} has an incomplete compensation contract"
        )
    try:
        definition = spec.compensation_builder(record, step, before_state)
        compensation_spec = TOOL_REGISTRY[definition.tool_name]
        validated_payload = compensation_spec.input_model.model_validate(
            definition.payload
        ).model_dump()
    except Exception as exc:
        raise CompensationPreparationError(
            f"cannot prepare compensation for {step.id}: {exc}"
        ) from exc

    suffix = step.id.removeprefix("step-")
    prepared = CompensationRecord(
        original_step_id=step.id,
        compensation_step_id=f"step-rollback-{suffix}",
        tool_name=definition.tool_name,
        tool_input=validated_payload,
        before_state=definition.before_state,
        expected_state=definition.expected_state,
        original_idempotency_key=engine.tool_executor.idempotency_key_for(
            step,
            run_id=record.id,
            plan_hash_value=record.policy.plan_hash,
        ),
        compensation_idempotency_key="pending",
    )
    compensation_step = _compensation_step(step, prepared)
    prepared.compensation_idempotency_key = engine.tool_executor.idempotency_key_for(
        compensation_step,
        run_id=record.id,
        plan_hash_value=record.policy.plan_hash,
    )
    record.compensations.append(prepared)
    engine.checkpoint(record, lease)
    return prepared


def mark_action_outcome(
    compensation: CompensationRecord | None,
    status: str,
    summary: str,
    error: str | None,
) -> None:
    if compensation is None:
        return
    compensation.updated_at = utc_now()
    compensation.result_summary = summary
    compensation.error = error
    if status in {"succeeded", "skipped"}:
        compensation.status = "ready"
    elif status == "unknown":
        compensation.status = "unknown"
        compensation.external_state_unknown = True
        compensation.manual_intervention_required = True
    else:
        compensation.status = "not_required"


def _audit_metadata(
    record: RunRecord,
    compensation: CompensationRecord,
    lease: LeaseContext,
    actor: UserIdentity,
    result: str,
    **extra: Any,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "run_id": record.id,
        "job_id": lease.job_id,
        "step_id": compensation.compensation_step_id,
        "original_step_id": compensation.original_step_id,
        "tool_name": compensation.tool_name,
        "actor": actor.username,
        "tenant_id": record.tenant_id,
        "plan_hash": record.policy.plan_hash,
        "idempotency_key": compensation.compensation_idempotency_key,
        "fencing_token": lease.fencing_token,
        "result": result,
    }
    metadata.update(extra)
    return metadata


def execute_compensation(
    engine: EnginePort,
    record: RunRecord,
    original: PlanStep,
    compensation: CompensationRecord,
    lease: LeaseContext,
    actor: UserIdentity,
) -> CompensationOutcome:
    if compensation.status == "succeeded":
        return CompensationOutcome(True, False, "compensation already succeeded")
    if compensation.status not in {"ready", "running"}:
        return CompensationOutcome(
            False,
            compensation.manual_intervention_required,
            f"compensation is not eligible: {compensation.status}",
        )

    compensation.status = "running"
    compensation.updated_at = utc_now()
    engine.audit(
        record,
        actor.username,
        "compensation.started",
        "开始逆序恢复已成功写操作的执行前状态。",
        _audit_metadata(record, compensation, lease, actor, "started"),
    )
    engine.checkpoint(record, lease)
    compensation_step = _compensation_step(original, compensation)
    result = engine.tool_executor.execute(
        compensation_step,
        run_id=record.id,
        plan_hash_value=record.policy.plan_hash or "missing",
        actor=actor,
        attempt=record.attempt,
        previous_results=record.tool_results,
        job_id=lease.job_id,
        fencing_token=lease.fencing_token,
        lease_guard=engine.lease_guard(lease),
    )
    record.tool_results.append(result)
    compensation.result_summary = result.summary
    compensation.error = result.error
    compensation.updated_at = utc_now()
    if result.status not in {"succeeded", "skipped"}:
        compensation.status = "unknown" if result.status == "unknown" else "failed"
        compensation.external_state_unknown = result.status == "unknown"
        compensation.manual_intervention_required = True
        engine.audit(
            record,
            actor.username,
            "compensation.failed",
            result.error or result.summary,
            _audit_metadata(
                record,
                compensation,
                lease,
                actor,
                result.status,
                capability_jti=result.capability_jti,
                error=result.error,
                external_state_unknown=compensation.external_state_unknown,
            ),
        )
        engine.checkpoint(record, lease)
        return CompensationOutcome(
            False, True, result.error or result.summary
        )

    try:
        fresh = engine.observe_before_execution(record, original, lease)
        record.observations.extend(fresh.observations)
        actual_state = {
            field: field_value(fresh.data, field)
            for field in compensation.expected_state
        }
        restored = actual_state == compensation.expected_state
        verification_error = None
    except PreExecutionObservationError as exc:
        actual_state = {}
        restored = False
        verification_error = str(exc)

    record.verification.append(
        VerificationResult(
            step_id=compensation.compensation_step_id,
            passed=restored,
            checks=[
                {
                    "field": field,
                    "operator": "eq",
                    "expected": expected,
                    "actual": actual_state.get(field),
                    "passed": actual_state.get(field) == expected,
                    "description": "独立只读通道确认补偿后的真实状态",
                }
                for field, expected in compensation.expected_state.items()
            ],
            summary=(
                "补偿动作已由独立状态读取确认。"
                if restored
                else "补偿动作没有通过独立状态确认。"
            ),
        )
    )
    if not restored:
        compensation.status = "failed"
        compensation.manual_intervention_required = True
        compensation.error = verification_error or "compensation verification failed"
        engine.audit(
            record,
            actor.username,
            "compensation.failed",
            compensation.error,
            _audit_metadata(
                record,
                compensation,
                lease,
                actor,
                "verification_failed",
                capability_jti=result.capability_jti,
                expected_state=compensation.expected_state,
                actual_state=actual_state,
            ),
        )
        engine.audit(
            record,
            actor.username,
            "rollback.verification_failed",
            compensation.error,
            {"original_step_id": original.id},
        )
        engine.checkpoint(record, lease)
        return CompensationOutcome(False, True, compensation.error)

    compensation.status = "succeeded"
    compensation.manual_intervention_required = False
    compensation.error = None
    engine.audit(
        record,
        actor.username,
        "compensation.succeeded",
        "补偿执行成功，并由独立只读通道确认外部状态已恢复。",
        _audit_metadata(
            record,
            compensation,
            lease,
            actor,
            "succeeded",
            capability_jti=result.capability_jti,
            expected_state=compensation.expected_state,
        ),
    )
    engine.audit(
        record,
        actor.username,
        "rollback.verified",
        "Saga compensation independently verified",
        {"original_step_id": original.id},
    )
    engine.checkpoint(record, lease)
    return CompensationOutcome(True, False, "external state restored")


def compensate_ready_actions(
    engine: EnginePort,
    record: RunRecord,
    lease: LeaseContext,
    actor: UserIdentity,
) -> CompensationOutcome:
    """Compensate known-successful writes in reverse plan order."""

    by_step = {step.id: step for step in record.plan}
    eligible = [
        item
        for item in reversed(record.compensations)
        if item.status in {"ready", "running"}
    ]
    if not eligible:
        return CompensationOutcome(True, False, "no committed write requires compensation")
    for compensation in eligible:
        original = by_step[compensation.original_step_id]
        outcome = execute_compensation(
            engine, record, original, compensation, lease, actor
        )
        if not outcome.restored:
            return outcome
    return CompensationOutcome(True, False, "all committed writes were restored")
