from app.metrics import build_metrics
from app.schemas import (
    Approval,
    EvaluationReport,
    Incident,
    ModelInvocation,
    RunRecord,
    SourceHit,
    ToolResult,
    TraceStatus,
    TraceStep,
    WorkerLeaseEvent,
)


def test_dashboard_metrics_distinguish_live_fallback_remote_and_recovery() -> None:
    run = RunRecord(
        id="RUN-METRICS",
        incident=Incident(
            title="metrics-api 测试异常",
            summary="用于验证指标口径的自由事件，不包含任何预期答案。",
            severity="P2",
            service="metrics-api",
            environment="lab",
            symptoms=["延迟升高"],
        ),
        status="completed",
        approval=Approval(required=True, decision="approved"),
        sources=[
            SourceHit(
                doc_id="RB-1",
                chunk_id="RB-1#C01",
                title="runbook",
                section="verify",
                excerpt="verify",
                score=0.9,
                uri="knowledge://RB-1",
            )
        ],
        traces=[
            TraceStep(
                id="T-1",
                node="verify",
                label="verify",
                status=TraceStatus.COMPLETED,
                summary="done",
                duration_ms=120,
            )
        ],
        model_calls=[
            ModelInvocation(
                id="M-1",
                provider="local",
                model="qwen",
                phase="investigation",
                status="succeeded",
                prompt_version="v3",
                latency_ms=200,
            ),
            ModelInvocation(
                id="M-2",
                provider="local",
                model="qwen",
                phase="remediation",
                status="fallback",
                prompt_version="v3",
                latency_ms=20,
            ),
        ],
        tool_results=[
            ToolResult(
                step_id="step-inspect",
                tool_name="inspect_logs",
                status="succeeded",
                summary="ok",
                idempotency_key="key",
                duration_ms=10,
                transport="remote-http-fault-lab",
            )
        ],
        lease_history=[
            WorkerLeaseEvent(worker_id="worker-2", fencing_token=2, action="recovered")
        ],
    )
    evaluation = EvaluationReport(
        id="EVAL-1",
        score=96.5,
        task_success_rate=90,
        root_cause_accuracy=90,
        tool_accuracy=100,
        retrieval_recall=100,
        safety_gate_accuracy=100,
        injection_resistance=100,
        unsafe_action_rate=0,
        capability_enforcement=100,
        suite_mode="sealed-fixture",
        cases=[],
    )
    metrics = build_metrics([run], evaluation)
    assert metrics.success_rate == 100
    assert metrics.model_live_rate == 50
    assert metrics.model_fallback_rate == 50
    assert metrics.average_model_latency_ms == 110
    assert metrics.remote_tool_rate == 100
    assert metrics.recovered_runs == 1
    assert metrics.evaluation_score == 96.5


def test_dashboard_metrics_handle_empty_input() -> None:
    metrics = build_metrics([], None)
    assert metrics.total_runs == 0
    assert metrics.success_rate == 0
    assert metrics.evaluation_score == 0

