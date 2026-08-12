from __future__ import annotations

from ..context import (
    EnginePort,
    LeaseContext,
    NodeOutcome,
    SYSTEM_AGENT,
    evaluate_check,
    topological,
)
from .execute import execution_actor
from ...schemas import (
    Check,
    PlanStep,
    RiskLevel,
    RollbackPlan,
    RunRecord,
    RunStatus,
    VerificationResult,
)
from ...registry import SERVICE_REGISTRY
from ...registry.contracts import observation_values, render_template
from ...tools import TOOL_REGISTRY


def execute_automatic_rollback(
    engine: EnginePort,
    record: RunRecord,
    step: PlanStep,
    lease: LeaseContext,
) -> tuple[bool, str]:
    if (
        step.rollback.mode != "tool"
        or not step.rollback.tool_name
        or not record.policy.plan_hash
    ):
        return False, "manual rollback required"
    acted = any(
        result.step_id == step.id and result.status in {"succeeded", "skipped"}
        for result in record.tool_results
    )
    if not acted:
        return True, "action did not commit; rollback not needed"
    spec = TOOL_REGISTRY[step.rollback.tool_name]
    suffix = step.id.removeprefix("step-")
    rollback_step = PlanStep(
        id=f"step-rollback-{suffix}",
        title="执行补偿回滚",
        objective="验证失败后恢复策略编译时记录的执行前状态。",
        tool_name=step.rollback.tool_name,
        tool_input=step.rollback.tool_input,
        evidence_ids=step.evidence_ids,
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
            rationale="补偿动作失败或结果未知时禁止递归自动回滚，立即由值班负责人接管。",
        ),
        risk=spec.risk,
        rationale="该补偿动作已包含在原始不可变计划哈希与人工审批范围内。",
    )
    actor = execution_actor(record)
    rollback_result = engine.tool_executor.execute(
        rollback_step,
        run_id=record.id,
        plan_hash_value=record.policy.plan_hash,
        actor=actor,
        attempt=record.attempt,
        previous_results=record.tool_results,
        job_id=lease.job_id,
        fencing_token=lease.fencing_token,
        lease_guard=engine.lease_guard(lease),
    )
    record.tool_results.append(rollback_result)
    engine.audit(
        record,
        actor.username,
        "rollback.executed",
        rollback_result.summary,
        {
            "original_step_id": step.id,
            "rollback_step_id": rollback_step.id,
            "tool": rollback_step.tool_name,
            "status": rollback_result.status,
            "capability_jti": rollback_result.capability_jti,
            "idempotency_key": rollback_result.idempotency_key,
        },
    )
    engine.checkpoint(record, lease)
    if rollback_result.status not in {"succeeded", "skipped"}:
        return False, rollback_result.error or rollback_result.summary

    target_replicas = int(step.rollback.tool_input["target_replicas"])
    kubernetes_scale = step.rollback.tool_name == "scale_kubernetes_deployment"
    verify_step = PlanStep(
        id=f"step-verify-rollback-{suffix}",
        title="复查回滚状态",
        objective="通过独立只读通道确认副本数已恢复到执行前状态。",
        tool_name=(
            "inspect_kubernetes_workload"
            if kubernetes_scale
            else "get_service_status"
        ),
        tool_input=(
            {
                "namespace": step.rollback.tool_input["namespace"],
                "deployment": step.rollback.tool_input["deployment"],
                "include_events": False,
                "event_limit": 0,
                "wait_for_ready_replicas": target_replicas,
                "timeout_seconds": 30,
            }
            if kubernetes_scale
            else {
                "experiment_id": record.incident.experiment_id,
                "service": record.incident.service,
                "include_instances": True,
            }
        ),
        evidence_ids=step.evidence_ids,
        success_criteria=[
            Check(
                field="replicas",
                operator="eq",
                value=target_replicas,
                description="副本数恢复到执行前观测值",
            )
        ],
        rollback=RollbackPlan(
            mode="manual",
            rationale="只读复查没有副作用；失败时由值班负责人直接检查编排平台。",
        ),
        risk=RiskLevel.LOW,
        rationale="不能把补偿工具的成功响应直接当成状态已经恢复。",
    )
    verify_result = engine.tool_executor.execute(
        verify_step,
        run_id=record.id,
        plan_hash_value=record.policy.plan_hash,
        actor=SYSTEM_AGENT,
        attempt=record.attempt,
        previous_results=record.tool_results,
        job_id=lease.job_id,
        fencing_token=lease.fencing_token,
        lease_guard=engine.lease_guard(lease),
    )
    record.tool_results.append(verify_result)
    actual = verify_result.output.get("replicas")
    passed = (
        verify_result.status in {"succeeded", "skipped"}
        and actual == target_replicas
    )
    record.verification.append(
        VerificationResult(
            step_id=rollback_step.id,
            passed=passed,
            checks=[
                {
                    "field": "replicas",
                    "operator": "eq",
                    "expected": target_replicas,
                    "actual": actual,
                    "passed": passed,
                    "description": "副本数恢复到执行前观测值",
                }
            ],
            summary=(
                "补偿动作已由独立状态读取确认。"
                if passed
                else "补偿动作没有通过独立状态确认。"
            ),
        )
    )
    engine.audit(
        record,
        "system",
        "rollback.verified" if passed else "rollback.verification_failed",
        f"rollback target replicas={target_replicas}, actual={actual}",
        {"original_step_id": step.id, "verification_step_id": verify_step.id},
    )
    engine.checkpoint(record, lease)
    return (
        passed,
        "rollback independently verified" if passed else "rollback verification failed",
    )


def run(engine: EnginePort, record: RunRecord, lease: LeaseContext) -> NodeOutcome:
    if not record.policy.plan_hash:
        raise ValueError("plan hash missing during verification")
    all_passed = True
    for step in record.plan:
        action_result = next(
            (
                result
                for result in reversed(record.tool_results)
                if result.step_id == step.id
                and result.status in {"succeeded", "skipped"}
            ),
            None,
        )
        if action_result is None:
            record.verification.append(
                VerificationResult(
                    step_id=step.id,
                    passed=False,
                    summary="找不到成功的动作结果。",
                )
            )
            all_passed = False
            continue
        kubernetes_scale = step.tool_name == "scale_kubernetes_deployment"
        service = SERVICE_REGISTRY.get(record.incident.service)
        registered_verification = (
            service.verification_observation
            if service is not None and not kubernetes_scale
            else None
        )
        verify_step = PlanStep(
            id=f"step-verify-{step.id.removeprefix('step-')}",
            title="复查原始指标",
            objective="重新读取原始指标，避免把工具返回的成功文本当成修复成功。",
            tool_name=(
                "inspect_kubernetes_workload"
                if kubernetes_scale
                else registered_verification.tool_name
                if registered_verification is not None
                else "query_metrics"
            ),
            tool_input=(
                {
                    "namespace": step.tool_input["namespace"],
                    "deployment": step.tool_input["deployment"],
                    "include_events": True,
                    "event_limit": 10,
                    "wait_for_ready_replicas": step.tool_input["target_replicas"],
                    "timeout_seconds": 30,
                }
                if kubernetes_scale
                else render_template(
                    registered_verification.input_template,
                    record.incident,
                    observation_values(record.observations),
                )
                if registered_verification is not None
                else {
                    "experiment_id": record.incident.experiment_id,
                    "service": record.incident.service,
                    "window_minutes": 5,
                }
            ),
            evidence_ids=step.evidence_ids,
            success_criteria=[
                Check(
                    field="__result_status__",
                    operator="eq",
                    value="succeeded",
                    description="验证指标读取成功",
                )
            ],
            rollback=RollbackPlan(
                mode="manual", rationale="验证失败时停止自动动作并人工复核。"
            ),
            risk=RiskLevel.LOW,
            rationale="修复是否成功必须回到原始观测通道确认。",
        )
        verify_result = engine.tool_executor.execute(
            verify_step,
            run_id=record.id,
            plan_hash_value=record.policy.plan_hash,
            actor=SYSTEM_AGENT,
            attempt=record.attempt,
            previous_results=record.tool_results,
            job_id=lease.job_id,
            fencing_token=lease.fencing_token,
            lease_guard=engine.lease_guard(lease),
        )
        record.tool_results.append(verify_result)
        context = {**action_result.output, **verify_result.output}
        checks: list[dict] = []
        passed = verify_result.status in {"succeeded", "skipped"}
        for check in step.success_criteria:
            check_passed, actual = evaluate_check(
                check, context, verify_result.status
            )
            passed = passed and check_passed
            checks.append(
                {
                    "field": check.field,
                    "operator": check.operator,
                    "expected": check.value,
                    "actual": actual,
                    "passed": check_passed,
                    "description": check.description,
                }
            )
        record.verification.append(
            VerificationResult(
                step_id=step.id,
                passed=passed,
                checks=checks,
                summary=(
                    "成功条件全部满足。" if passed else "至少一个成功条件不满足。"
                ),
            )
        )
        all_passed = all_passed and passed
        engine.checkpoint(record, lease)
    if not all_passed:
        automatic_outcomes: list[bool] = []
        manual_required = False
        for step in reversed(topological(record.plan)):
            if step.rollback.mode == "tool":
                passed, _ = execute_automatic_rollback(engine, record, step, lease)
                automatic_outcomes.append(passed)
            else:
                manual_required = True
                engine.audit(
                    record,
                    "system",
                    "rollback.required",
                    step.rollback.rationale,
                    {"step_id": step.id, "mode": "manual"},
                )
        if automatic_outcomes and all(automatic_outcomes) and not manual_required:
            record.error_code = "VERIFICATION_FAILED_ROLLED_BACK"
            record.error_detail = (
                "主动作没有满足成功条件；所有补偿动作已执行并由独立只读通道确认。"
            )
        elif automatic_outcomes and not all(automatic_outcomes):
            record.error_code = "ROLLBACK_FAILED"
            record.error_detail = (
                "主动作验证失败，且至少一个自动补偿动作未能确认恢复。"
            )
        else:
            record.error_code = "VERIFICATION_FAILED_MANUAL_ROLLBACK_REQUIRED"
            record.error_detail = (
                "主动作没有满足成功条件；计划要求人工执行或复核回滚。"
            )
        return NodeOutcome(
            record.error_detail,
            "verification=false",
            status=RunStatus.FAILED,
        )
    return NodeOutcome(
        "所有写动作均由独立指标读取验证。",
        "verification=true",
        next_node="finalize",
    )
