from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from ..schemas import Check, Incident, Observation, RiskLevel


TemplateValue = Any


def observation_values(observations: list[Observation]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for observation in observations:
        values.update(observation.data)
    return values


def render_template(
    value: TemplateValue,
    incident: Incident,
    observed: Mapping[str, Any] | None = None,
) -> TemplateValue:
    """Resolve a deliberately small, data-only binding language."""
    context = observed or {}
    if isinstance(value, str) and value.startswith("$incident."):
        return getattr(incident, value.removeprefix("$incident."))
    if isinstance(value, str) and value.startswith("$observation."):
        key = value.removeprefix("$observation.")
        if key not in context:
            raise KeyError(f"observation value is missing: {key}")
        return context[key]
    if isinstance(value, dict):
        return {
            key: render_template(item, incident, context)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [render_template(item, incident, context) for item in value]
    return value


@dataclass(frozen=True)
class ObservationDefinition:
    id: str
    title: str
    objective: str
    tool_name: str
    input_template: Mapping[str, TemplateValue]
    rationale: str
    success_criteria: tuple[Check, ...] = ()


@dataclass(frozen=True)
class RemediationDefinition:
    id: str
    title: str
    objective: str
    diagnosis: str
    confidence: float
    tool_name: str
    input_template: Mapping[str, TemplateValue]
    match_checks: tuple[Check, ...]
    preconditions: tuple[Check, ...]
    success_criteria: tuple[Check, ...]
    risk: RiskLevel
    rationale: str
    rollback_mode: Literal["manual", "tool"] = "manual"
    rollback_tool_name: str | None = None
    rollback_input_template: Mapping[str, TemplateValue] = field(default_factory=dict)
    rollback_rationale: str = "由值班负责人根据执行前证据人工回滚。"


@dataclass(frozen=True)
class ServiceDefinition:
    service_id: str
    display_name: str
    runbook_ids: tuple[str, ...]
    allowed_observations: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    environment: frozenset[str]
    observation_steps: tuple[ObservationDefinition, ...]
    verification_observation: ObservationDefinition | None = None
    fixture_remediations: tuple[RemediationDefinition, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunbookDefinition:
    runbook_id: str
    title: str
    knowledge_path: str
    service_ids: tuple[str, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)
