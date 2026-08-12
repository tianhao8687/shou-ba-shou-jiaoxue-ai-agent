from __future__ import annotations

from ..context import EnginePort, LeaseContext, NodeOutcome
from ...schemas import RunRecord


def run(engine: EnginePort, record: RunRecord, lease: LeaseContext) -> NodeOutcome:
    del engine, lease
    return NodeOutcome(
        summary="自由事件已规范化；运行时对象不包含预期根因、预期计划或预期结果。",
        output=record.incident.summary,
        next_node="retrieve",
    )
