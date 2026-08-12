from __future__ import annotations

from ..context import EnginePort, LeaseContext, NodeOutcome
from ...schemas import RunRecord, RunStatus


def run(engine: EnginePort, record: RunRecord, lease: LeaseContext) -> NodeOutcome:
    del lease
    actions = [
        result.summary
        for result in record.tool_results
        if result.step_id in {step.id for step in record.plan}
        and result.status in {"succeeded", "skipped"}
    ]
    record.resolution = "；".join(actions) or "验证完成，没有执行写动作。"
    engine.audit(
        record,
        "system",
        "run.completed",
        "隐藏真值未参与运行；成功来自工具状态和条件验证。",
    )
    return NodeOutcome(
        "运行完成并归档。",
        record.resolution,
        next_node="completed",
        status=RunStatus.COMPLETED,
    )
