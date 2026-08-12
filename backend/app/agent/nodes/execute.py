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
from ...schemas import RunRecord, RunStatus, UserIdentity
from ...security import AuthorizationError, require_all_roles


def execution_actor(record: RunRecord) -> UserIdentity:
    if not record.approval.required:
        return SYSTEM_AGENT
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
        precondition_context = observation_context(record)
        failed_preconditions = [
            check.description
            for check in step.preconditions
            if not evaluate_check(check, precondition_context, "succeeded")[0]
        ]
        if failed_preconditions:
            record.resolution = (
                f"执行前置条件已变化：{'; '.join(failed_preconditions)}"
            )
            return NodeOutcome(
                record.resolution,
                "precondition_changed",
                next_node="handed_off",
                status=RunStatus.HANDED_OFF,
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
        engine.audit(
            record,
            actor.username,
            "tool.executed",
            result.summary,
            {
                "tool": step.tool_name,
                "step_id": step.id,
                "status": result.status,
                "capability_jti": result.capability_jti,
                "idempotency_key": result.idempotency_key,
            },
        )
        engine.checkpoint(record, lease)
        if result.status == "unknown":
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
