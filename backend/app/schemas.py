from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    HANDED_OFF = "handed_off"
    CANCELLED = "cancelled"


class JobStatus(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TraceStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    WAITING = "waiting"
    BLOCKED = "blocked"
    FAILED = "failed"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Incident(StrictModel):
    """Runtime input. Deliberately contains no expected answer or pre-written plan."""

    title: str = Field(min_length=6, max_length=160)
    summary: str = Field(min_length=12, max_length=4000)
    severity: Literal["P1", "P2", "P3"]
    service: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    environment: Literal["production", "staging", "lab"] = "lab"
    symptoms: list[str] = Field(min_length=1, max_length=12)
    experiment_id: str | None = Field(default=None, pattern=r"^EXP-[A-Z0-9]{8,32}$")
    tags: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("symptoms")
    @classmethod
    def clean_symptoms(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if not cleaned:
            raise ValueError("at least one non-empty symptom is required")
        if any(len(value) > 300 for value in cleaned):
            raise ValueError("a symptom cannot exceed 300 characters")
        return cleaned


class SourceHit(StrictModel):
    chunk_id: str
    doc_id: str
    title: str
    section: str
    excerpt: str
    score: float = Field(ge=0, le=1)
    uri: str
    retrieval_channel: Literal["lexical", "semantic", "hybrid", "routed", "pinned"] = "hybrid"


class Observation(StrictModel):
    id: str
    step_id: str
    tool_name: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    captured_at: datetime = Field(default_factory=utc_now)
    transport: str = "unknown"
    source_uri: str | None = None


class Check(StrictModel):
    field: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,120}$")
    operator: Literal["eq", "ne", "lt", "lte", "gt", "gte", "contains", "in"]
    value: Any
    description: str = Field(min_length=4, max_length=240)


class RollbackPlan(StrictModel):
    mode: Literal["tool", "manual"]
    tool_name: str | None = None
    tool_input: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=8, max_length=500)

    @model_validator(mode="after")
    def validate_tool_mode(self) -> "RollbackPlan":
        if self.mode == "tool" and not self.tool_name:
            raise ValueError("tool rollback requires tool_name")
        if self.mode == "manual" and (self.tool_name or self.tool_input):
            raise ValueError("manual rollback cannot contain executable tool fields")
        return self


class PlanStep(StrictModel):
    id: str = Field(pattern=r"^step-[a-z0-9-]{2,80}$")
    title: str = Field(min_length=4, max_length=120)
    objective: str = Field(min_length=8, max_length=500)
    tool_name: str = Field(min_length=2, max_length=80)
    tool_input: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    depends_on: list[str] = Field(default_factory=list, max_length=12)
    preconditions: list[Check] = Field(default_factory=list, max_length=8)
    success_criteria: list[Check] = Field(min_length=1, max_length=8)
    rollback: RollbackPlan
    risk: RiskLevel = RiskLevel.LOW
    rationale: str = Field(min_length=8, max_length=700)

    @model_validator(mode="after")
    def no_self_dependency(self) -> "PlanStep":
        if self.id in self.depends_on:
            raise ValueError("a plan step cannot depend on itself")
        return self


class PolicyIssue(StrictModel):
    code: str
    step_id: str | None = None
    message: str
    blocking: bool = True


class PolicyDecision(StrictModel):
    accepted: bool = False
    plan_hash: str | None = None
    effective_risk: RiskLevel = RiskLevel.LOW
    required_roles: list[str] = Field(default_factory=list)
    issues: list[PolicyIssue] = Field(default_factory=list)
    compiled_at: datetime = Field(default_factory=utc_now)


class ToolResult(StrictModel):
    step_id: str
    tool_name: str
    status: Literal["succeeded", "failed", "skipped", "unknown"]
    summary: str
    output: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    duration_ms: int = 0
    error: str | None = None
    attempt: int = 1
    transport: str = "unknown"
    capability_jti: str | None = None
    job_id: str | None = None
    fencing_token: int | None = Field(default=None, ge=1)


class VerificationResult(StrictModel):
    step_id: str
    passed: bool
    checks: list[dict[str, Any]] = Field(default_factory=list)
    summary: str


class TraceStep(StrictModel):
    id: str
    node: str
    label: str
    status: TraceStatus
    started_at: datetime = Field(default_factory=utc_now)
    duration_ms: int = 0
    summary: str
    input_preview: str = ""
    output_preview: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class AuditEvent(StrictModel):
    id: str
    timestamp: datetime = Field(default_factory=utc_now)
    actor: str
    action: str
    detail: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalVote(StrictModel):
    approver: str
    display_name: str
    roles: list[str]
    tenant_id: str
    plan_hash: str
    decision: Literal["approve", "deny"]
    note: str = Field(default="", max_length=500)
    decided_at: datetime = Field(default_factory=utc_now)


class Approval(StrictModel):
    required: bool = False
    decision: Literal["pending", "approved", "denied", "not_required"] = "not_required"
    plan_hash: str | None = None
    required_roles: list[str] = Field(default_factory=list)
    required_approvals: int = Field(default=0, ge=0, le=4)
    separation_of_duties: bool = False
    requester: str | None = None
    votes: list[ApprovalVote] = Field(default_factory=list, max_length=4)
    decided_by: str | None = None
    decided_roles: list[str] = Field(default_factory=list)
    decided_at: datetime | None = None
    note: str | None = None


class ModelProposal(StrictModel):
    phase: Literal["investigation", "remediation"]
    diagnosis: str = Field(min_length=8, max_length=1600)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    plan: list[PlanStep] = Field(default_factory=list, max_length=12)
    safety_notes: list[str] = Field(default_factory=list, max_length=8)
    needs_handoff: bool = False
    handoff_reason: str | None = Field(default=None, max_length=500)


class ModelInvocation(StrictModel):
    id: str
    provider: str
    model: str
    phase: Literal["investigation", "remediation"]
    status: Literal["succeeded", "fallback", "failed", "disabled"]
    schema_name: str = "ModelProposal"
    prompt_version: str
    latency_ms: int = 0
    queue_wait_ms: int = Field(default=0, ge=0)
    input_characters: int = 0
    output_characters: int = 0
    error: str | None = None
    fallback_reason: str | None = None


class WorkerLeaseEvent(StrictModel):
    worker_id: str
    fencing_token: int
    action: Literal["claimed", "heartbeat", "released", "recovered"]
    timestamp: datetime = Field(default_factory=utc_now)


class RunRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    id: str
    incident: Incident
    tenant_id: str = Field(
        default="xm-ops",
        pattern=r"^[a-z0-9][a-z0-9-]{1,63}$",
    )
    created_by: str = "system"
    status: RunStatus = RunStatus.QUEUED
    run_mode: Literal["live-model", "test-fixture", "model-degraded"] = "live-model"
    current_node: str = "queued"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    version: int = Field(default=0, ge=0)
    attempt: int = 0
    max_attempts: int = 3
    cancellation_requested: bool = False
    diagnosis: str = ""
    confidence: float = Field(default=0.0, ge=0, le=1)
    model_proposal: ModelProposal | None = None
    model_calls: list[ModelInvocation] = Field(default_factory=list)
    sources: list[SourceHit] = Field(default_factory=list)
    investigation_plan: list[PlanStep] = Field(default_factory=list)
    investigation_policy: PolicyDecision = Field(default_factory=PolicyDecision)
    observations: list[Observation] = Field(default_factory=list)
    plan: list[PlanStep] = Field(default_factory=list)
    policy: PolicyDecision = Field(default_factory=PolicyDecision)
    risk_level: RiskLevel = RiskLevel.LOW
    approval: Approval = Field(default_factory=Approval)
    tool_results: list[ToolResult] = Field(default_factory=list)
    verification: list[VerificationResult] = Field(default_factory=list)
    resolution: str = ""
    traces: list[TraceStep] = Field(default_factory=list)
    audit: list[AuditEvent] = Field(default_factory=list)
    lease_history: list[WorkerLeaseEvent] = Field(default_factory=list)
    error_code: str | None = None
    error_detail: str | None = None
    estimated_cost: float = 0.0


class JobRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=True, extra="forbid")

    id: str
    run_id: str
    stage: Literal["start", "resume", "retry"]
    status: JobStatus = JobStatus.QUEUED
    owner: str | None = None
    lease_until: datetime | None = None
    fencing_token: int = 0
    attempts: int = 0
    available_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_error: str | None = None


class StartRunRequest(StrictModel):
    incident: Incident


class DecisionRequest(StrictModel):
    decision: Literal["approve", "deny"]
    note: str = Field(default="", max_length=500)
    expected_version: int = Field(ge=0)


class LoginRequest(StrictModel):
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=200)


class UserIdentity(StrictModel):
    username: str
    display_name: str
    roles: list[str]
    tenant_id: str = Field(
        default="xm-ops",
        pattern=r"^[a-z0-9][a-z0-9-]{1,63}$",
    )


class AuthResponse(StrictModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    user: UserIdentity


class EvaluationCaseResult(StrictModel):
    case_id: str
    variant_id: str = "baseline"
    variant_category: str = "baseline"
    attack_surface: str = "incident_text"
    passed: bool
    fault_kind: str
    expected_tool: str | None = None
    selected_tools: list[str] = Field(default_factory=list)
    root_cause_match: bool
    outcome_match: bool
    retrieval_hit: bool
    gate_correct: bool
    injection_resistant: bool
    unsafe_action: bool
    capability_enforced: bool
    latency_ms: int = 0
    notes: list[str] = Field(default_factory=list)


class EvaluationReport(StrictModel):
    id: str
    tenant_id: str = Field(
        default="xm-ops",
        pattern=r"^[a-z0-9][a-z0-9-]{1,63}$",
    )
    created_at: datetime = Field(default_factory=utc_now)
    score: float
    task_success_rate: float
    root_cause_accuracy: float
    tool_accuracy: float
    retrieval_recall: float
    safety_gate_accuracy: float
    injection_resistance: float
    unsafe_action_rate: float
    capability_enforcement: float
    p95_case_latency_ms: int = 0
    suite_mode: Literal["sealed-fixture", "sealed-live-model"]
    suite_version: str = "v3"
    suite_fingerprint: str = ""
    case_count: int = 0
    passed_count: int = 0
    confidence_level: float = 0.95
    task_success_ci_lower: float = 0.0
    task_success_ci_upper: float = 0.0
    category_breakdown: dict[str, dict[str, float | int]] = Field(
        default_factory=dict
    )
    cases: list[EvaluationCaseResult]


class DashboardMetrics(StrictModel):
    total_runs: int
    completed_runs: int
    queued_runs: int
    running_runs: int
    awaiting_approval: int
    recovered_runs: int
    success_rate: float
    tool_success_rate: float
    approval_rate: float
    citation_coverage: float
    p95_latency_ms: int
    model_live_rate: float = 0.0
    model_fallback_rate: float = 0.0
    average_model_latency_ms: int = 0
    remote_tool_rate: float = 0.0
    unsafe_action_rate: float = 0.0
    evaluation_score: float = 0.0
    trend: list[dict[str, Any]] = Field(default_factory=list)


class HealthResponse(StrictModel):
    status: str
    ready: bool
    purpose: Literal["runtime-status", "readiness"] = "runtime-status"
    app: str
    version: str
    mode: str
    database: str
    database_status: dict[str, Any] = Field(default_factory=dict)
    vector_backend: str
    vector_quality: Literal["semantic", "lexical-feature-baseline", "unavailable"]
    knowledge_documents: int
    knowledge_chunks: int = 0
    model_runtime: dict[str, Any] = Field(default_factory=dict)
    tool_runtime: dict[str, Any] = Field(default_factory=dict)
    worker_runtime: dict[str, Any] = Field(default_factory=dict)
    readiness_checks: dict[str, bool] = Field(default_factory=dict)


class DrillRequest(StrictModel):
    fault_kind: Literal[
        "connection_pool_exhaustion",
        "queue_backlog",
        "expired_credential",
        "stale_cache",
        "dependency_rate_limit",
    ]


class DrillDescriptor(StrictModel):
    experiment_id: str
    incident: Incident
