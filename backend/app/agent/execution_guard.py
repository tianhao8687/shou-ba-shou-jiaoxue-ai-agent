from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .compensation import CompensationPreparationError, prepare_compensation
from .context import EnginePort, LeaseContext, NodeOutcome, evaluate_check
from .pre_execution import (
    FreshObservation,
    PreExecutionObservationError,
    PreExecutionObservationInvalid,
)
from ..schemas import CompensationRecord, PlanStep, RunRecord, RunStatus, UserIdentity
from ..tools import TOOL_REGISTRY


@dataclass(frozen=True)
class WriteExecutionGuard:
    fresh: FreshObservation | None = None
    compensation: CompensationRecord | None = None
    blocked: NodeOutcome | None = None


def step_target(step: PlanStep) -> dict[str, Any]:
    keys = (
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
    return {key: step.tool_input[key] for key in keys if key in step.tool_input}


def execution_audit_metadata(
    record: RunRecord,
    step: PlanStep,
    lease: LeaseContext,
    actor: str,
    *,
    idempotency_key: str,
    result: str,
    source: str = "not-observed",
    observed_at: str | None = None,
    target: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    metadata = {
        "run_id": record.id,
        "job_id": lease.job_id,
        "step_id": step.id,
        "tool_name": step.tool_name,
        "actor": actor,
        "tenant_id": record.tenant_id,
        "plan_hash": record.policy.plan_hash,
        "idempotency_key": idempotency_key,
        "fencing_token": lease.fencing_token,
        "target": target if target is not None else step_target(step),
        "source": source,
        "observed_at": observed_at,
        "result": result,
    }
    metadata.update(extra)
    return metadata


def guard_write_execution(
    engine: EnginePort,
    record: RunRecord,
    step: PlanStep,
    lease: LeaseContext,
    actor: UserIdentity,
) -> WriteExecutionGuard:
    """Freshly observe, revalidate and durably prepare an inverse before a write."""

    spec = TOOL_REGISTRY[step.tool_name]
    engine.audit(
        record,
        actor.username,
        "precondition.revalidation.started",
        "开始执行写操作前的最新状态观测。",
        execution_audit_metadata(
            record,
            step,
            lease,
            actor.username,
            idempotency_key="not-issued",
            result="started",
            source=",".join(
                item.tool_name for item in spec.pre_execution_observations
            ),
        ),
    )
    try:
        fresh = engine.observe_before_execution(record, step, lease)
    except PreExecutionObservationInvalid as exc:
        record.error_code = "PRE_EXECUTION_OBSERVATION_INVALID"
        record.error_detail = str(exc)[:1000]
        record.resolution = (
            "执行前最新观测数据无效；未使用旧观测兜底，已转人工。"
        )
        engine.audit(
            record,
            actor.username,
            "precondition.revalidation.failed",
            record.resolution,
            execution_audit_metadata(
                record,
                step,
                lease,
                actor.username,
                idempotency_key="not-issued",
                result="invalid",
                error=record.error_detail,
            ),
        )
        return WriteExecutionGuard(
            blocked=NodeOutcome(
                record.resolution,
                record.error_detail,
                next_node="handed_off",
                status=RunStatus.HANDED_OFF,
            )
        )
    except PreExecutionObservationError as exc:
        record.error_code = "PRE_EXECUTION_OBSERVATION_FAILED"
        record.error_detail = str(exc)[:1000]
        record.resolution = (
            "执行前最新观测失败；未使用旧观测兜底，已转人工。"
        )
        engine.audit(
            record,
            actor.username,
            "precondition.revalidation.failed",
            record.resolution,
            execution_audit_metadata(
                record,
                step,
                lease,
                actor.username,
                idempotency_key="not-issued",
                result="failed",
                error=record.error_detail,
            ),
        )
        return WriteExecutionGuard(
            blocked=NodeOutcome(
                record.resolution,
                record.error_detail,
                next_node="handed_off",
                status=RunStatus.HANDED_OFF,
            )
        )

    record.observations.extend(fresh.observations)
    failed_preconditions = [
        check.description
        for check in step.preconditions
        if not evaluate_check(check, fresh.data, "succeeded")[0]
    ]
    if failed_preconditions:
        record.error_code = "PRECONDITION_CHANGED"
        record.error_detail = "; ".join(failed_preconditions)
        record.resolution = (
            f"执行前置条件已变化：{'; '.join(failed_preconditions)}"
        )
        metadata = execution_audit_metadata(
            record,
            step,
            lease,
            actor.username,
            idempotency_key=fresh.idempotency_key,
            result="changed",
            source=fresh.source,
            observed_at=fresh.observed_at.isoformat(),
            target=fresh.target,
            failed_preconditions=failed_preconditions,
        )
        engine.audit(
            record,
            actor.username,
            "precondition.changed",
            record.resolution,
            metadata,
        )
        engine.audit(
            record,
            actor.username,
            "precondition.revalidation.failed",
            record.resolution,
            metadata,
        )
        return WriteExecutionGuard(
            fresh=fresh,
            blocked=NodeOutcome(
                record.resolution,
                record.error_code,
                next_node="handed_off",
                status=RunStatus.HANDED_OFF,
            ),
        )

    engine.audit(
        record,
        actor.username,
        "precondition.revalidation.succeeded",
        "最新状态观测完成，全部执行前置条件仍成立。",
        execution_audit_metadata(
            record,
            step,
            lease,
            actor.username,
            idempotency_key=fresh.idempotency_key,
            result="succeeded",
            source=fresh.source,
            observed_at=fresh.observed_at.isoformat(),
            target=fresh.target,
        ),
    )
    engine.checkpoint(record, lease)
    try:
        compensation = prepare_compensation(
            engine, record, step, fresh.data, lease
        )
    except CompensationPreparationError as exc:
        record.error_code = "COMPENSATION_PREPARATION_FAILED"
        record.error_detail = str(exc)[:1000]
        record.resolution = (
            "无法从执行前真实状态生成确定性补偿；未执行写操作，已转人工。"
        )
        engine.audit(
            record,
            actor.username,
            "compensation.failed",
            record.resolution,
            execution_audit_metadata(
                record,
                step,
                lease,
                actor.username,
                idempotency_key="not-issued",
                result="preparation_failed",
                source=fresh.source,
                observed_at=fresh.observed_at.isoformat(),
                target=fresh.target,
                error=record.error_detail,
            ),
        )
        return WriteExecutionGuard(
            fresh=fresh,
            blocked=NodeOutcome(
                record.resolution,
                record.error_detail,
                next_node="handed_off",
                status=RunStatus.HANDED_OFF,
            ),
        )
    return WriteExecutionGuard(fresh=fresh, compensation=compensation)
