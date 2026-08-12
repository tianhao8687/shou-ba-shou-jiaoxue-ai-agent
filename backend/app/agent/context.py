from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable, Protocol

from ..schemas import Check, PlanStep, RunRecord, RunStatus, UserIdentity
from ..store import LeaseLostError


SYSTEM_AGENT = UserIdentity(
    username="agent@harbor.local",
    display_name="Harbor 只读执行身份",
    roles=["observer", "operator"],
)


NODE_LABELS = {
    "intake": "事件接收",
    "retrieve": "知识检索",
    "investigate": "调查计划",
    "observe": "工具观测",
    "diagnose": "诊断与修复计划",
    "policy": "策略编译",
    "gate": "风险门",
    "execute": "受控执行",
    "verify": "结果验证",
    "finalize": "完成归档",
    "approval": "人工审批",
    "handed_off": "人工接管",
    "completed": "完成",
    "cancelled": "已取消",
}


@dataclass(frozen=True)
class LeaseContext:
    job_id: str
    worker_id: str
    fencing_token: int
    recovered: bool = False
    lost_event: threading.Event | None = None

    def assert_active(self) -> None:
        if self.lost_event is not None and self.lost_event.is_set():
            raise LeaseLostError(
                f"job {self.job_id} lease was invalidated during execution"
            )


@dataclass(frozen=True)
class NodeOutcome:
    """Typed result returned by every workflow node.

    Nodes may mutate their own business payload on ``run`` but they cannot choose an
    arbitrary next state by assigning ``run.current_node``.  The lifecycle layer
    validates and persists this outcome through the state machine.
    """

    summary: str
    output: str
    next_node: str | None = None
    status: RunStatus | None = None
    reason: str = ""


class EnginePort(Protocol):
    store: Any
    retriever: Any
    tool_executor: Any
    model_adapter: Any
    policy_compiler: Any
    medium_risk_approval_quorum: int
    high_risk_approval_quorum: int
    enforce_requester_separation: bool
    production_observation_enabled: bool
    kubernetes_connector_enabled: bool
    kubernetes_staging_namespace: str

    def checkpoint(self, run: RunRecord, lease: LeaseContext) -> RunRecord: ...

    def lease_guard(self, lease: LeaseContext) -> Callable[[], None]: ...


NodeHandler = Callable[[EnginePort, RunRecord, LeaseContext], NodeOutcome]


def topological(plan: list[PlanStep]) -> list[PlanStep]:
    by_id = {step.id: step for step in plan}
    pending = {step.id: set(step.depends_on) for step in plan}
    ordered: list[PlanStep] = []
    while pending:
        ready = sorted(
            step_id for step_id, dependencies in pending.items() if not dependencies
        )
        if not ready:
            raise ValueError("plan contains a dependency cycle")
        for step_id in ready:
            ordered.append(by_id[step_id])
            pending.pop(step_id)
            for dependencies in pending.values():
                dependencies.discard(step_id)
    return ordered


def observation_context(run: RunRecord) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for observation in run.observations:
        context.update(observation.data)
    return context


def field_value(context: dict[str, Any], path: str) -> Any:
    value: Any = context
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def evaluate_check(
    check: Check, context: dict[str, Any], result_status: str
) -> tuple[bool, Any]:
    actual = (
        result_status
        if check.field == "__result_status__"
        else field_value(context, check.field)
    )
    expected = check.value
    try:
        if check.operator == "eq":
            return actual == expected, actual
        if check.operator == "ne":
            return actual != expected, actual
        if check.operator == "lt":
            return actual < expected, actual
        if check.operator == "lte":
            return actual <= expected, actual
        if check.operator == "gt":
            return actual > expected, actual
        if check.operator == "gte":
            return actual >= expected, actual
        if check.operator == "contains":
            return expected in actual, actual
        if check.operator == "in":
            return actual in expected, actual
    except (TypeError, ValueError):
        return False, actual
    return False, actual
