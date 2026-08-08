from __future__ import annotations

import pytest

from app.policy import PolicyCompiler
from app.schemas import Check, PlanStep, RiskLevel, RollbackPlan, UserIdentity
from app.security import AuthorizationError, CapabilityService


def restart_step(*, evidence_ids: list[str], risk: RiskLevel = RiskLevel.LOW) -> PlanStep:
    return PlanStep(
        id="step-restart-one",
        title="重启一个实例",
        objective="只重启观测到的单个异常实例并验证指标。",
        tool_name="restart_service",
        tool_input={
            "experiment_id": "EXP-12345678",
            "service": "checkout-api",
            "instance": "checkout-api-1",
            "strategy": "single-instance",
        },
        evidence_ids=evidence_ids,
        success_criteria=[
            Check(field="p95_ms", operator="lte", value=800, description="延迟恢复")
        ],
        rollback=RollbackPlan(mode="manual", rationale="失败时由值班负责人切走实例流量。"),
        risk=risk,
        rationale="指标和日志共同指向单实例连接池问题。",
    )


def test_policy_rejects_unknown_evidence_and_upgrades_declared_risk() -> None:
    compiler = PolicyCompiler()
    _, rejected = compiler.compile(
        [restart_step(evidence_ids=["OBS-FAKE"])],
        phase="remediation",
        allowed_evidence_ids={"OBS-REAL"},
        observation_ids={"OBS-REAL"},
        experiment_id="EXP-12345678",
        observation_context={"pool_waiters": 42},
    )
    assert rejected.accepted is False
    assert {issue.code for issue in rejected.issues} >= {
        "UNKNOWN_EVIDENCE",
        "WRITE_WITHOUT_OBSERVATION",
    }

    compiled, accepted = compiler.compile(
        [restart_step(evidence_ids=["OBS-REAL"], risk=RiskLevel.LOW)],
        phase="remediation",
        allowed_evidence_ids={"OBS-REAL"},
        observation_ids={"OBS-REAL"},
        experiment_id="EXP-12345678",
        observation_context={"pool_waiters": 42},
    )
    assert accepted.accepted is True
    assert compiled[0].risk == RiskLevel.HIGH
    assert any(issue.code == "RISK_UPGRADED" and not issue.blocking for issue in accepted.issues)


def test_policy_rejects_a_write_tool_without_applicable_observation() -> None:
    compiler = PolicyCompiler()
    _, decision = compiler.compile(
        [restart_step(evidence_ids=["OBS-REAL"])],
        phase="remediation",
        allowed_evidence_ids={"OBS-REAL"},
        observation_ids={"OBS-REAL"},
        experiment_id="EXP-12345678",
        observation_context={"queue_depth": 9000},
    )
    assert decision.accepted is False
    assert "TOOL_NOT_SUPPORTED_BY_OBSERVATION" in {
        issue.code for issue in decision.issues
    }


def test_policy_compiles_automatic_rollback_into_hash_and_rejects_smuggling() -> None:
    compiler = PolicyCompiler()
    step = PlanStep(
        id="step-scale-workers",
        title="扩容队列消费者",
        objective="按观测到的吞吐差计算目标副本，并在失败时恢复原容量。",
        tool_name="scale_workers",
        tool_input={
            "experiment_id": "EXP-12345678",
            "service": "invoice-worker",
            "target_replicas": 5,
            "change_ticket": "CHG-9001",
        },
        evidence_ids=["OBS-QUEUE"],
        success_criteria=[
            Check(field="trend", operator="eq", value="falling", description="积压转为下降")
        ],
        rollback=RollbackPlan(
            mode="tool",
            tool_name="scale_workers",
            tool_input={
                "experiment_id": "EXP-12345678",
                "service": "invoice-worker",
                "target_replicas": 2,
                "change_ticket": "CHG-9001",
            },
            rationale="验证失败时恢复到执行前观测到的两个副本。",
        ),
        risk=RiskLevel.MEDIUM,
        rationale="当前消费容量低于生产速率且实例健康。",
    )
    compiled, accepted = compiler.compile(
        [step],
        phase="remediation",
        allowed_evidence_ids={"OBS-QUEUE"},
        observation_ids={"OBS-QUEUE"},
        experiment_id="EXP-12345678",
        observation_context={"queue_depth": 9000, "oldest_age_s": 1200},
    )
    assert accepted.accepted is True
    assert accepted.plan_hash
    assert compiled[0].rollback.tool_input["target_replicas"] == 2
    assert "on-call-lead" in accepted.required_roles

    smuggled = step.model_copy(
        update={
            "rollback": RollbackPlan(
                mode="tool",
                tool_name="restart_service",
                tool_input={
                    "experiment_id": "EXP-12345678",
                    "service": "invoice-worker",
                    "instance": "invoice-worker-1",
                    "strategy": "single-instance",
                },
                rationale="试图利用回滚字段夹带另一个高风险工具。",
            )
        }
    )
    _, rejected = compiler.compile(
        [smuggled],
        phase="remediation",
        allowed_evidence_ids={"OBS-QUEUE"},
        observation_ids={"OBS-QUEUE"},
        experiment_id="EXP-12345678",
        observation_context={"queue_depth": 9000, "oldest_age_s": 1200},
    )
    assert rejected.accepted is False
    codes = {issue.code for issue in rejected.issues}
    assert {"ROLLBACK_TOOL_MISMATCH", "AUTOMATIC_ROLLBACK_NOT_ALLOWED"} <= codes


def test_capability_is_bound_to_tool_payload_and_role() -> None:
    service = CapabilityService("capability-test-secret-is-long-enough", ttl_seconds=90)
    lead = UserIdentity(
        username="lead@harbor.local",
        display_name="lead",
        roles=["observer", "operator", "on-call-lead"],
    )
    payload = restart_step(evidence_ids=["OBS-REAL"]).tool_input
    grant = service.issue(
        run_id="RUN-1",
        plan_hash_value="a" * 64,
        step_id="step-restart-one",
        tool_name="restart_service",
        payload=payload,
        actor=lead,
        required_role="on-call-lead",
    )
    claims = service.verify(grant.token, tool_name="restart_service", payload=payload)
    assert claims["run_id"] == "RUN-1"
    assert claims["tenant_id"] == "xm-ops"
    with pytest.raises(AuthorizationError, match="tenant"):
        service.verify(
            grant.token,
            tool_name="restart_service",
            payload=payload,
            tenant_id="other-tenant",
        )
    with pytest.raises(AuthorizationError, match="payload"):
        service.verify(
            grant.token,
            tool_name="restart_service",
            payload={**payload, "instance": "checkout-api-2"},
        )
    with pytest.raises(AuthorizationError, match="tool"):
        service.verify(grant.token, tool_name="rotate_credential", payload=payload)
