from __future__ import annotations

import time
from typing import Any, Callable, ContextManager, Literal, Protocol

from ..observability import MODEL_CALLS, MODEL_LATENCY, MODEL_SLOT_WAIT
from ..schemas import Incident, ModelInvocation, ModelProposal, Observation, SourceHit

class ModelAdapter(Protocol):
    def propose(
        self,
        *,
        phase: Literal["investigation", "remediation"],
        incident: Incident,
        sources: list[SourceHit],
        observations: list[Observation],
    ) -> tuple[ModelProposal, ModelInvocation]: ...

    def health(self) -> dict[str, Any]: ...


class CoordinatedModelAdapter:
    """Keep a single-capacity local model from timing out under worker fan-out."""

    def __init__(
        self,
        delegate: ModelAdapter,
        slot_factory: Callable[[], ContextManager[None]],
        coordination: str,
    ) -> None:
        self.delegate = delegate
        self.slot_factory = slot_factory
        self.coordination = coordination

    def propose(
        self,
        *,
        phase: Literal["investigation", "remediation"],
        incident: Incident,
        sources: list[SourceHit],
        observations: list[Observation],
    ) -> tuple[ModelProposal, ModelInvocation]:
        waiting_started = time.perf_counter()
        with self.slot_factory():
            queue_wait_ms = round((time.perf_counter() - waiting_started) * 1000)
            MODEL_SLOT_WAIT.observe(queue_wait_ms / 1000)
            proposal, invocation = self.delegate.propose(
                phase=phase,
                incident=incident,
                sources=sources,
                observations=observations,
            )
        invocation.queue_wait_ms = queue_wait_ms
        MODEL_CALLS.labels(provider=invocation.provider, status=invocation.status).inc()
        MODEL_LATENCY.labels(provider=invocation.provider).observe(invocation.latency_ms / 1000)
        return proposal, invocation

    def health(self) -> dict[str, Any]:
        result = dict(self.delegate.health())
        result["concurrency_control"] = self.coordination
        return result
