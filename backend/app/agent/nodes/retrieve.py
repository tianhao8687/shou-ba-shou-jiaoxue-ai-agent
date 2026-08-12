from __future__ import annotations

from ..context import EnginePort, LeaseContext, NodeOutcome
from ...registry import SERVICE_REGISTRY
from ...schemas import RunRecord


def run(engine: EnginePort, record: RunRecord, lease: LeaseContext) -> NodeOutcome:
    del lease
    query = " ".join(
        [record.incident.service, record.incident.title, *record.incident.symptoms]
    )
    service = SERVICE_REGISTRY.get(record.incident.service)
    record.sources = engine.retriever.search(
        query,
        limit=6,
        preferred_doc_ids=list(service.runbook_ids) if service else [],
    )
    return NodeOutcome(
        summary=f"检索到 {len(record.sources)} 个去重文档片段。",
        output=", ".join(item.chunk_id for item in record.sources),
        next_node="investigate",
    )
