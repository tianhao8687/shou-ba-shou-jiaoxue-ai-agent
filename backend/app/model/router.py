from __future__ import annotations

import time
from typing import Any, Literal
from uuid import uuid4

from ..schemas import Incident, ModelInvocation, ModelProposal, Observation, SourceHit
from .adapter import ModelAdapter
from .fixture import SafeDisabledModelAdapter
from .openai_compatible import OpenAICompatibleModelAdapter
from .prompts import PROMPT_VERSION

class ResilientModelRouter:
    """Live model with fail-closed fallback; never substitutes a canned diagnosis."""

    def __init__(self, live: OpenAICompatibleModelAdapter, breaker_seconds: float = 30.0) -> None:
        self.live = live
        self.breaker_seconds = breaker_seconds
        self._unavailable_until = 0.0

    def _failure(
        self,
        phase: Literal["investigation", "remediation"],
        sources: list[SourceHit],
        reason: str,
        error: str | None = None,
        latency_ms: int = 0,
    ) -> tuple[ModelProposal, ModelInvocation]:
        proposal = ModelProposal(
            phase=phase,
            diagnosis="本地模型调用失败；系统已关闭写动作并请求人工接管。",
            confidence=0,
            evidence_ids=[source.chunk_id for source in sources[:2]],
            plan=[],
            safety_notes=["fail closed: model failure never enables a tool"],
            needs_handoff=True,
            handoff_reason=reason,
        )
        return proposal, ModelInvocation(
            id=f"MC-{uuid4().hex[:8].upper()}",
            provider=self.live.provider,
            model=self.live.model,
            phase=phase,
            status="fallback",
            prompt_version=PROMPT_VERSION,
            latency_ms=latency_ms,
            error=error,
            fallback_reason=reason,
        )

    def propose(
        self,
        *,
        phase: Literal["investigation", "remediation"],
        incident: Incident,
        sources: list[SourceHit],
        observations: list[Observation],
    ) -> tuple[ModelProposal, ModelInvocation]:
        if time.monotonic() < self._unavailable_until:
            return self._failure(phase, sources, "local model circuit breaker is open")
        started = time.perf_counter()
        try:
            return self.live.propose(
                phase=phase, incident=incident, sources=sources, observations=observations
            )
        except Exception as exc:
            self._unavailable_until = time.monotonic() + self.breaker_seconds
            return self._failure(
                phase,
                sources,
                "local structured-output call failed validation or transport",
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
                latency_ms=round((time.perf_counter() - started) * 1000),
            )

    def health(self) -> dict[str, Any]:
        result = self.live.health()
        result["circuit_open"] = time.monotonic() < self._unavailable_until
        return result


def create_model_adapter(
    *,
    enabled: bool,
    endpoint: str,
    model: str,
    provider: str,
    timeout_seconds: float,
    api_key: str = "",
) -> ModelAdapter:
    if not enabled:
        return SafeDisabledModelAdapter("MODEL_ENABLED=false")
    return ResilientModelRouter(
        OpenAICompatibleModelAdapter(endpoint, model, provider, timeout_seconds, api_key)
    )
