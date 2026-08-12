from __future__ import annotations

from uuid import uuid4

from ..context import EnginePort, LeaseContext, NodeOutcome, SYSTEM_AGENT, topological
from ...schemas import Observation, RunRecord, RunStatus


def run(engine: EnginePort, record: RunRecord, lease: LeaseContext) -> NodeOutcome:
    plan_hash_value = record.investigation_policy.plan_hash
    if not plan_hash_value:
        raise ValueError("investigation plan hash is missing")
    for step in topological(record.investigation_plan):
        if any(observation.step_id == step.id for observation in record.observations):
            continue
        result = engine.tool_executor.execute(
            step,
            run_id=record.id,
            plan_hash_value=plan_hash_value,
            actor=SYSTEM_AGENT,
            attempt=record.attempt,
            previous_results=record.tool_results,
            job_id=lease.job_id,
            fencing_token=lease.fencing_token,
            lease_guard=engine.lease_guard(lease),
        )
        record.tool_results.append(result)
        if result.status not in {"succeeded", "skipped"}:
            record.resolution = f"只读观测失败或结果未知：{result.summary}"
            engine.checkpoint(record, lease)
            return NodeOutcome(
                record.resolution,
                result.error or result.summary,
                next_node="handed_off",
                status=RunStatus.HANDED_OFF,
            )
        observation = Observation(
            id=f"OBS-{uuid4().hex[:10].upper()}",
            step_id=step.id,
            tool_name=step.tool_name,
            summary=result.summary,
            data=result.output,
            transport=result.transport,
            source_uri=result.output.get("source_uri"),
        )
        record.observations.append(observation)
        engine.checkpoint(record, lease)
        if (
            step.tool_name == "query_prometheus_slo"
            and result.output.get("sample_count") == 0
        ):
            record.resolution = (
                "Prometheus 查询成功，但目标服务标签没有返回可归因指标；"
                "未调用模型、未执行写动作，已转人工核对指标标签与采集状态。"
            )
            return NodeOutcome(
                record.resolution,
                "empty Prometheus observation",
                next_node="handed_off",
                status=RunStatus.HANDED_OFF,
            )
    return NodeOutcome(
        f"收集到 {len(record.observations)} 条独立工具观测。",
        ", ".join(item.id for item in record.observations),
        next_node="diagnose",
    )
