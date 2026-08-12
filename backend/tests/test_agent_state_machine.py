from datetime import timedelta

import pytest

from app.agent.transitions import (
    InvalidTransitionError,
    NodeRetryBudgetExceeded,
    WorkflowDeadlineExceeded,
    apply_transition,
    assert_within_deadline,
    consume_node_attempt,
)
from app.schemas import Incident, RunRecord, utc_now


def run_at(node: str = "intake") -> RunRecord:
    return RunRecord(
        id="RUN-STATE-MACHINE",
        incident=Incident(
            title="订单服务错误率突增",
            summary="订单 API 在实验环境持续出现可观测的错误率突增。",
            severity="P2",
            service="order-api",
            environment="lab",
            symptoms=["HTTP 5xx 高于基线"],
            experiment_id="EXP-STATE001",
        ),
        current_node=node,
    )


def test_illegal_transition_fails_closed_without_mutating_run() -> None:
    run = run_at("intake")

    with pytest.raises(InvalidTransitionError, match="intake -> execute"):
        apply_transition(
            run,
            "execute",
            actor="test",
            reason="attempt to bypass evidence and policy nodes",
        )

    assert run.current_node == "intake"
    assert run.transition_count == 0
    assert run.traces == []
    assert run.audit == []


def test_legal_transition_is_counted_traced_and_audited() -> None:
    run = run_at("intake")

    apply_transition(
        run,
        "retrieve",
        actor="workflow",
        reason="intake contract satisfied",
    )

    assert run.current_node == "retrieve"
    assert run.transition_count == 1
    assert run.traces[-1].node == "intake->retrieve"
    assert run.traces[-1].metadata == {
        "from": "intake",
        "to": "retrieve",
        "transition_count": 1,
    }
    assert run.audit[-1].action == "workflow.transition"
    assert run.audit[-1].metadata["to"] == "retrieve"


def test_node_specific_retry_budget_replaces_magic_loop_limit() -> None:
    run = run_at("retrieve")

    assert consume_node_attempt(run, "retrieve") == 1
    assert consume_node_attempt(run, "retrieve") == 2
    with pytest.raises(NodeRetryBudgetExceeded, match="retry budget 2"):
        consume_node_attempt(run, "retrieve")

    assert run.node_attempts == {"retrieve": 2}


def test_expired_workflow_deadline_is_rejected_before_transition() -> None:
    run = run_at("intake")
    run.workflow_deadline_at = utc_now() - timedelta(milliseconds=1)

    with pytest.raises(WorkflowDeadlineExceeded):
        assert_within_deadline(run)
