from __future__ import annotations

import json

from ..context import EnginePort, LeaseContext, NodeOutcome
from ...schemas import Approval, RiskLevel, RunRecord, RunStatus


def run(engine: EnginePort, record: RunRecord, lease: LeaseContext) -> NodeOutcome:
    del lease
    if not record.policy.plan_hash:
        raise ValueError("compiled plan hash is missing")
    requires_approval = record.risk_level in {
        RiskLevel.MEDIUM,
        RiskLevel.HIGH,
        "medium",
        "high",
    }
    if requires_approval:
        required_approvals = (
            engine.high_risk_approval_quorum
            if record.risk_level in {RiskLevel.HIGH, "high"}
            else engine.medium_risk_approval_quorum
        )
        record.approval = Approval(
            required=True,
            decision="pending",
            plan_hash=record.policy.plan_hash,
            required_roles=record.policy.required_roles,
            required_approvals=required_approvals,
            separation_of_duties=engine.enforce_requester_separation,
            requester=record.created_by,
        )
        engine.audit(
            record,
            "risk-gate",
            "approval.requested",
            "中高风险动作已暂停，等待不同主体满足审批 quorum。",
            {
                "plan_hash": record.policy.plan_hash,
                "roles": record.policy.required_roles,
                "required_approvals": required_approvals,
                "requester": record.created_by,
            },
        )
        return NodeOutcome(
            "风险门暂停了执行；尚未产生写副作用。",
            json.dumps(record.policy.required_roles, ensure_ascii=False),
            next_node="approval",
            status=RunStatus.AWAITING_APPROVAL,
        )
    record.approval = Approval(
        required=False,
        decision="not_required",
        plan_hash=record.policy.plan_hash,
        required_roles=record.policy.required_roles,
        required_approvals=0,
        separation_of_duties=False,
        requester=record.created_by,
    )
    return NodeOutcome(
        "低风险计划可由受限系统身份执行。",
        record.policy.plan_hash,
        next_node="execute",
    )
