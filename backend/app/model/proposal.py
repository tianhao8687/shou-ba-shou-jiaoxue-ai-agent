from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import RollbackPlan

class CompactDraftStep(BaseModel):
    """Small model-facing DSL. The server enriches it into the full Plan IR."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=2, max_length=80)
    tool_name: str = Field(min_length=2, max_length=80)
    tool_input: dict[str, Any]
    evidence_ids: list[str] = Field(min_length=1, max_length=8)
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    rationale: str = Field(min_length=8, max_length=500)


class CompactDraftProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: Literal["investigation", "remediation"]
    diagnosis: str = Field(min_length=8, max_length=1400)
    confidence: float = Field(ge=0, le=1)
    steps: list[CompactDraftStep] = Field(default_factory=list, max_length=6)
    safety_notes: list[str] = Field(default_factory=list, max_length=6)
    needs_handoff: bool = False
    handoff_reason: str | None = Field(default=None, max_length=500)

def _manual_rollback(reason: str) -> RollbackPlan:
    return RollbackPlan(mode="manual", rationale=reason)
