from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import PlanStep, RiskLevel, RunRecord

class StrictToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LabTargetInput(StrictToolInput):
    experiment_id: str = Field(pattern=r"^EXP-[A-Z0-9]{8,32}$")
    service: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")


class QueryMetricsInput(LabTargetInput):
    window_minutes: int = Field(ge=1, le=120)


class PrometheusServiceInput(StrictToolInput):
    service: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    environment: Literal["production", "staging"]
    window_minutes: int = Field(ge=1, le=120)


class KubernetesTargetInput(StrictToolInput):
    namespace: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    deployment: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")


class InspectKubernetesWorkloadInput(KubernetesTargetInput):
    include_events: bool = True
    event_limit: int = Field(default=10, ge=0, le=30)
    wait_for_ready_replicas: int | None = Field(default=None, ge=1, le=5)
    timeout_seconds: int = Field(default=0, ge=0, le=30)


class ScaleKubernetesDeploymentInput(KubernetesTargetInput):
    target_replicas: int = Field(ge=1, le=5)
    change_ticket: str = Field(pattern=r"^CHG-\d{4,}$")


class InspectLogsInput(LabTargetInput):
    query: str = Field(min_length=2, max_length=160)
    limit: int = Field(ge=1, le=100)


class GetServiceStatusInput(LabTargetInput):
    include_instances: bool = True


class RestartServiceInput(LabTargetInput):
    instance: str = Field(min_length=2, max_length=120)
    strategy: str = Field(pattern=r"^(single-instance|rolling)$")


class ScaleWorkersInput(LabTargetInput):
    target_replicas: int = Field(ge=1, le=30)
    change_ticket: str = Field(pattern=r"^CHG-\d{4,}$")


class RotateCredentialInput(LabTargetInput):
    credential_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,80}$")
    target_version: str = Field(pattern=r"^v\d+$")
    scope: str = Field(pattern=r"^(canary|single-tenant)$")


class RefreshCacheInput(LabTargetInput):
    tenant: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,80}$")
    category: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,80}$")


@dataclass(frozen=True)
class PreExecutionObservationSpec:
    """Deterministic read route required immediately before a write tool."""

    tool_name: str
    payload_builder: Callable[[RunRecord, PlanStep], dict[str, Any]]
    required_fields: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ToolCompensation:
    """A deterministic inverse action built by the tool contract, never by an LLM."""

    tool_name: str
    payload: dict[str, Any]
    before_state: dict[str, Any]
    expected_state: dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk: RiskLevel
    input_model: type[StrictToolInput]
    required_role: str
    read_only: bool
    applicability: str
    rollback_contract: Literal["none", "manual", "same-tool"] = "manual"
    idempotency_policy: Literal["read-only", "hash-bound"] = "hash-bound"
    required_observation_fields: frozenset[str] = frozenset()
    pre_execution_observations: tuple[PreExecutionObservationSpec, ...] = ()
    compensatable: bool = False
    compensation_builder: Callable[
        [RunRecord, PlanStep, dict[str, Any]], ToolCompensation
    ] | None = None

    def __post_init__(self) -> None:
        if self.read_only and self.pre_execution_observations:
            raise ValueError(
                f"read-only tool {self.name} cannot require a pre-execution write guard"
            )
        if not self.read_only and not self.pre_execution_observations:
            raise ValueError(
                f"write tool {self.name} must declare a fresh observation contract"
            )
        if self.read_only and self.compensatable:
            raise ValueError(f"read-only tool {self.name} cannot be compensatable")
        if self.compensatable != (self.compensation_builder is not None):
            raise ValueError(
                f"tool {self.name} must declare compensatable and builder together"
            )

@dataclass(frozen=True)
class ToolCallResponse:
    status: str
    summary: str
    output: dict[str, Any]
    error: str | None = None


class ToolClient(Protocol):
    name: str

    def invoke(
        self,
        tool_name: str,
        payload: dict[str, Any],
        idempotency_key: str,
        capability_token: str,
        tenant_id: str,
        job_id: str,
        fencing_token: int,
    ) -> ToolCallResponse: ...

    def health(self) -> dict[str, Any]: ...
