from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..schemas import RiskLevel

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
