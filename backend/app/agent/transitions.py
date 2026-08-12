from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping
from uuid import uuid4

from ..schemas import (
    RunRecord,
    RunStatus,
    TraceStatus,
    TraceStep,
    utc_now,
)
from ..store.audit import append_audit_event
from .context import NODE_LABELS


class InvalidTransitionError(RuntimeError):
    pass


class NodeRetryBudgetExceeded(RuntimeError):
    pass


class WorkflowDeadlineExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class NodeContract:
    name: str
    allowed_next: frozenset[str]
    retry_budget: int
    retry_rationale: str


# These edges are derived from the current executable workflow.  Terminal failure
# keeps the failing node for safe retry, so FAILED is a RunStatus rather than a fake
# node edge.  Cancellation is accepted from every non-terminal node below.
NODE_CONTRACTS: Mapping[str, NodeContract] = {
    "intake": NodeContract("intake", frozenset({"retrieve"}), 1, "pure input normalization"),
    "retrieve": NodeContract("retrieve", frozenset({"investigate"}), 2, "read-only index access can be retried once"),
    "investigate": NodeContract("investigate", frozenset({"observe", "handed_off"}), 1, "deterministic plan compilation"),
    "observe": NodeContract("observe", frozenset({"diagnose", "handed_off"}), 2, "read-only tools and persisted partial observations"),
    "diagnose": NodeContract("diagnose", frozenset({"policy", "handed_off"}), 2, "bounded model transport retry with no side effect"),
    "policy": NodeContract("policy", frozenset({"gate", "handed_off"}), 1, "deterministic fail-closed compiler"),
    "gate": NodeContract("gate", frozenset({"approval", "execute"}), 1, "deterministic risk decision"),
    "approval": NodeContract("approval", frozenset({"execute", "handed_off"}), 1, "human decision is version-bound, not looped"),
    "execute": NodeContract("execute", frozenset({"verify", "handed_off"}), 2, "same hash-bound idempotency key is mandatory"),
    "verify": NodeContract("verify", frozenset({"finalize"}), 2, "read-only verification and idempotent compensation"),
    "finalize": NodeContract("finalize", frozenset({"completed"}), 1, "pure terminal projection"),
    "handed_off": NodeContract("handed_off", frozenset(), 0, "terminal"),
    "completed": NodeContract("completed", frozenset(), 0, "terminal"),
    "cancelled": NodeContract("cancelled", frozenset(), 0, "terminal"),
}


TERMINAL_NODES = frozenset({"handed_off", "completed", "cancelled"})
ACTIVE_NODES = frozenset(NODE_CONTRACTS) - TERMINAL_NODES
GLOBAL_SAFETY_TRANSITION_GUARD = 64
DEFAULT_WORKFLOW_TIMEOUT_SECONDS = 30 * 60


def ensure_deadline(run: RunRecord, timeout_seconds: int) -> datetime:
    if run.workflow_deadline_at is None:
        run.workflow_deadline_at = run.created_at + timedelta(seconds=timeout_seconds)
    return run.workflow_deadline_at


def assert_within_deadline(run: RunRecord, now: datetime | None = None) -> None:
    if run.workflow_deadline_at is None:
        return
    if (now or utc_now()) >= run.workflow_deadline_at:
        raise WorkflowDeadlineExceeded(
            f"run {run.id} exceeded workflow deadline {run.workflow_deadline_at.isoformat()}"
        )


def consume_node_attempt(run: RunRecord, node: str) -> int:
    contract = NODE_CONTRACTS.get(node)
    if contract is None or contract.retry_budget <= 0:
        raise NodeRetryBudgetExceeded(f"node {node} has no executable retry budget")
    used = int(run.node_attempts.get(node, 0))
    if used >= contract.retry_budget:
        raise NodeRetryBudgetExceeded(
            f"node {node} exhausted retry budget {contract.retry_budget}: "
            f"{contract.retry_rationale}"
        )
    run.node_attempts[node] = used + 1
    return used + 1


def remaining_node_attempts(run: RunRecord, node: str) -> int:
    contract = NODE_CONTRACTS.get(node)
    if contract is None:
        return 0
    return max(0, contract.retry_budget - int(run.node_attempts.get(node, 0)))


def apply_transition(
    run: RunRecord,
    target: str,
    *,
    actor: str,
    reason: str,
    status: RunStatus | None = None,
) -> None:
    source = run.current_node
    contract = NODE_CONTRACTS.get(source)
    if contract is None:
        raise InvalidTransitionError(f"unknown workflow source node: {source}")
    cancellation = target == "cancelled" and source in ACTIVE_NODES
    if not cancellation and target not in contract.allowed_next:
        raise InvalidTransitionError(f"illegal workflow transition: {source} -> {target}")
    if run.transition_count >= GLOBAL_SAFETY_TRANSITION_GUARD:
        raise InvalidTransitionError(
            "global safety transition guard reached after explicit edge validation"
        )

    run.current_node = target
    run.transition_count += 1
    if status is not None:
        run.status = status
    metadata = {
        "from": source,
        "to": target,
        "transition_count": run.transition_count,
    }
    run.traces.append(
        TraceStep(
            id=f"TR-{uuid4().hex[:10].upper()}",
            node=f"{source}->{target}",
            label="状态转换",
            status=TraceStatus.COMPLETED,
            summary=reason,
            metadata=metadata,
        )
    )
    append_audit_event(
        run,
        actor,
        "workflow.transition",
        reason,
        metadata,
    )
