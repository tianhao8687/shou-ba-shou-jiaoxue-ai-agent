from __future__ import annotations

from ..context import EnginePort, LeaseContext, NodeOutcome
from ...schemas import RunRecord, RunStatus


def run(engine: EnginePort, record: RunRecord, lease: LeaseContext) -> NodeOutcome:
    planning_sources = [
        source
        for source in record.sources
        if source.retrieval_channel in {"routed", "pinned"}
    ]
    engine.lease_guard(lease)()
    proposal, invocation = engine.model_adapter.propose(
        phase="remediation",
        incident=record.incident,
        sources=planning_sources,
        observations=record.observations,
    )
    engine.lease_guard(lease)()
    record.model_calls.append(invocation)
    record.model_proposal = proposal
    record.diagnosis = proposal.diagnosis
    record.confidence = proposal.confidence
    record.plan = proposal.plan
    if invocation.status == "fallback":
        record.run_mode = "model-degraded"
    staging_kubernetes = (
        not record.incident.experiment_id
        and record.incident.environment == "staging"
        and engine.kubernetes_connector_enabled
    )
    if not record.incident.experiment_id and not staging_kubernetes:
        record.plan = []
        record.resolution = (
            "已完成生产 Prometheus 只读取证和模型诊断；当前未配置生产写连接器，"
            "因此不会把实验室动作映射到真实系统，已携带证据转人工。"
        )
        return NodeOutcome(
            record.resolution,
            proposal.diagnosis,
            next_node="handed_off",
            status=RunStatus.HANDED_OFF,
        )
    if proposal.needs_handoff:
        record.resolution = (
            "没有证据支持白名单内的安全修复："
            f"{proposal.handoff_reason or proposal.diagnosis}"
        )
        return NodeOutcome(
            record.resolution,
            proposal.diagnosis,
            next_node="handed_off",
            status=RunStatus.HANDED_OFF,
        )
    return NodeOutcome(
        "模型基于工具观测生成了动态修复计划。",
        proposal.diagnosis,
        next_node="policy",
    )
