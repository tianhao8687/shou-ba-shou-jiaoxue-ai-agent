from __future__ import annotations

from ..context import (
    EnginePort,
    LeaseContext,
    NodeOutcome,
    SYSTEM_AGENT,
    evaluate_check,
    observation_context,
    topological,
)
from ..compensation import (
    compensate_ready_actions,
    mark_action_outcome,
)
from ..execution_guard import execution_audit_metadata, guard_write_execution
from ...schemas import RunRecord, RunStatus, UserIdentity
from ...security import AuthorizationError, require_all_roles
from ...tools import TOOL_REGISTRY

def execution_actor(record: RunRecord) -> UserIdentity:
    if not record.approval.required:
        return SYSTEM_AGENT.model_copy(update={"tenant_id": record.tenant_id})
    if (
        record.approval.decision != "approved"
        or record.approval.plan_hash != record.policy.plan_hash
    ):
        raise AuthorizationError("plan is not approved or approval hash is stale")
    if len(record.approval.votes) < record.approval.required_approvals:
        raise AuthorizationError("approval quorum is incomplete")
    if any(
        vote.plan_hash != record.policy.plan_hash for vote in record.approval.votes
    ):
        raise AuthorizationError("approval vote is bound to a stale plan hash")
    actor = UserIdentity(
        username=record.approval.decided_by or "unknown",
        display_name=record.approval.decided_by or "unknown",
        roles=record.approval.decided_roles,
        tenant_id=record.tenant_id,
    )
    require_all_roles(actor, record.policy.required_roles)
    return actor


def run(engine: EnginePort, record: RunRecord, lease: LeaseContext) -> NodeOutcome:
    if not record.policy.accepted or not record.policy.plan_hash:
        raise ValueError("uncompiled plan cannot execute")
    actor = execution_actor(record)
    completed_step_ids = {
        result.step_id
        for result in record.tool_results
        if result.status in {"succeeded", "skipped"}
    }
    for step in topological(record.plan):
        if step.id in completed_step_ids:
            continue
        if not set(step.depends_on).issubset(completed_step_ids):
            raise ValueError(f"dependencies have not succeeded for {step.id}")
        spec = TOOL_REGISTRY.get(step.tool_name)
        if spec is None:
            raise ValueError(f"tool disappeared after policy compilation: {step.tool_name}")
        fresh = None
        compensation = None
        if spec.read_only:
            failed_preconditions = [
                check.description
                for check in step.preconditions
                if not evaluate_check(
                    check, observation_context(record), "succeeded"
                )[0]
            ]
            if failed_preconditions:
                record.error_code = "PRECONDITION_CHANGED"
                record.error_detail = "; ".join(failed_preconditions)
                record.resolution = (
                    f"执行前置条件已变化：{'; '.join(failed_preconditions)}"
                )
                return NodeOutcome(
                    record.resolution,
                    record.error_code,
                    next_node="handed_off",
                    status=RunStatus.HANDED_OFF,
                )
        else:
            guarded = guard_write_execution(engine, record, step, lease, actor)
            if guarded.blocked is not None:
                return guarded.blocked
            fresh = guarded.fresh
            compensation = guarded.compensation

        action_idempotency_key = engine.tool_executor.idempotency_key_for(
            step,
            run_id=record.id,
            plan_hash_value=record.policy.plan_hash,
        )
        engine.audit(
            record,
            actor.username,
            "tool.execution.started",
            "租约与围栏令牌验证完成，准备签发动作能力令牌。",
            execution_audit_metadata(
                record,
                step,
                lease,
                actor.username,
                idempotency_key=action_idempotency_key,
                result="started",
                source=fresh.source if fresh is not None else "workflow",
                observed_at=(
                    fresh.observed_at.isoformat() if fresh is not None else None
                ),
                target=fresh.target if fresh is not None else None,
            ),
        )
        result = engine.tool_executor.execute(
            step,
            run_id=record.id,
            plan_hash_value=record.policy.plan_hash,
            actor=actor,
            attempt=record.attempt,
            previous_results=record.tool_results,
            job_id=lease.job_id,
            fencing_token=lease.fencing_token,
            lease_guard=engine.lease_guard(lease),
        )
        record.tool_results.append(result)
        mark_action_outcome(
            compensation, result.status, result.summary, result.error
        )
        audit_action = {
            "unknown": "tool.execution.unknown",
            "failed": "tool.execution.failed",
        }.get(result.status, "tool.execution.succeeded")
        engine.audit(
            record,
            actor.username,
            audit_action,
            result.summary,
            execution_audit_metadata(
                record,
                step,
                lease,
                actor.username,
                idempotency_key=result.idempotency_key,
                result=result.status,
                source=result.transport,
                observed_at=(
                    fresh.observed_at.isoformat() if fresh is not None else None
                ),
                target=fresh.target if fresh is not None else None,
                capability_jti=result.capability_jti,
                error=result.error,
            ),
        )
        engine.checkpoint(record, lease)
        if result.status == "unknown":
            record.error_code = "TOOL_EXECUTION_UNKNOWN"
            record.error_detail = result.error or result.summary
            record.resolution = (
                "写操作结果未知；禁止换幂等键继续，已转人工核对。"
            )
            return NodeOutcome(
                record.resolution,
                result.error or "unknown",
                next_node="handed_off",
                status=RunStatus.HANDED_OFF,
            )
        if result.status == "failed":
            record.error_code = "TOOL_EXECUTION_FAILED"
            record.error_detail = result.error or result.summary
            has_committed_actions = any(
                item.status in {"ready", "running"}
                for item in record.compensations
            )
            if has_committed_actions:
                compensation_outcome = compensate_ready_actions(
                    engine, record, lease, actor
                )
                if compensation_outcome.restored:
                    record.error_code = "PARTIAL_FAILURE_COMPENSATED"
                    record.error_detail = (
                        "后续工具明确失败；此前成功的写操作已按逆序补偿并独立验证。"
                    )
                    record.resolution = record.error_detail
                    return NodeOutcome(
                        record.resolution,
                        compensation_outcome.detail,
                        status=RunStatus.FAILED,
                    )
                record.error_code = "COMPENSATION_FAILED"
                record.error_detail = compensation_outcome.detail
                record.resolution = (
                    "部分写入后的自动补偿失败或结果未知；需要人工确认外部状态。"
                )
                return NodeOutcome(
                    record.resolution,
                    record.error_detail,
                    next_node="handed_off",
                    status=RunStatus.HANDED_OFF,
                )
            return NodeOutcome(
                "工具明确失败，工作流停止。",
                record.error_detail,
                status=RunStatus.FAILED,
            )
        completed_step_ids.add(step.id)
    return NodeOutcome(
        "所有计划动作已执行或被持久幂等记录确认。",
        f"{len(record.plan)} steps",
        next_node="verify",
    )
