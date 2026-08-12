from __future__ import annotations

from ..context import EnginePort, LeaseContext, NodeOutcome, observation_context
from ...schemas import RunRecord, RunStatus


def run(engine: EnginePort, record: RunRecord, lease: LeaseContext) -> NodeOutcome:
    del lease
    allowed = (
        {source.chunk_id for source in record.sources}
        | {observation.id for observation in record.observations}
        | {"incident:input"}
    )
    observation_ids = {observation.id for observation in record.observations}
    compiled, decision = engine.policy_compiler.compile(
        record.plan,
        phase="remediation",
        allowed_evidence_ids=allowed,
        observation_ids=observation_ids,
        experiment_id=record.incident.experiment_id,
        service_id=record.incident.service,
        observation_context=observation_context(record),
    )
    record.plan = compiled
    record.policy = decision
    record.risk_level = decision.effective_risk
    if not decision.accepted:
        record.resolution = "动态计划未通过策略编译，没有执行写动作。"
        engine.audit(
            record,
            "policy-compiler",
            "plan.rejected",
            record.resolution,
            {"issues": [issue.model_dump(mode="json") for issue in decision.issues]},
        )
        return NodeOutcome(
            record.resolution,
            "; ".join(issue.code for issue in decision.issues),
            next_node="handed_off",
            status=RunStatus.HANDED_OFF,
        )
    engine.audit(
        record,
        "policy-compiler",
        "plan.accepted",
        "动态计划通过确定性策略编译。",
        {"plan_hash": decision.plan_hash, "required_roles": decision.required_roles},
    )
    return NodeOutcome(
        "工具、参数、证据、依赖、风险和回滚检查通过。",
        decision.plan_hash or "",
        next_node="gate",
    )
