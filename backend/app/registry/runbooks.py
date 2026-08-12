from __future__ import annotations

from collections.abc import Iterator, Mapping

from .contracts import RunbookDefinition


class RunbookRegistry(Mapping[str, RunbookDefinition]):
    def __init__(self) -> None:
        self._items: dict[str, RunbookDefinition] = {}

    def register(self, definition: RunbookDefinition, *, replace: bool = False) -> None:
        if definition.runbook_id in self._items and not replace:
            raise ValueError(f"runbook already registered: {definition.runbook_id}")
        self._items[definition.runbook_id] = definition

    def __getitem__(self, runbook_id: str) -> RunbookDefinition:
        return self._items[runbook_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


RUNBOOK_REGISTRY = RunbookRegistry()

for definition in (
    RunbookDefinition("RB-101", "连接池耗尽处置", "knowledge/RB-101-checkout-latency.md", ("checkout-api",)),
    RunbookDefinition("RB-104", "消费者积压处置", "knowledge/RB-104-worker-backlog.md", ("invoice-worker",)),
    RunbookDefinition("RB-203", "合作方认证失败处置", "knowledge/RB-203-partner-auth.md", ("partner-gateway",)),
    RunbookDefinition("RB-310", "租户缓存漂移处置", "knowledge/RB-310-catalog-cache.md", ("catalog-api",)),
    RunbookDefinition("RB-429", "依赖限流处置", "knowledge/RB-429-dependency-rate-limit.md", ("recommendation-api",)),
    RunbookDefinition("OPS-12", "人工接管要求", "knowledge/OPS-12-verification-and-rollback.md", ("recommendation-api",)),
):
    RUNBOOK_REGISTRY.register(definition)
