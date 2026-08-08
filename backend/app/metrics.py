from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import ceil

from .schemas import DashboardMetrics, EvaluationReport, RunRecord


def _percent(numerator: int, denominator: int) -> float:
    return round(100 * numerator / denominator, 1) if denominator else 0.0


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[max(0, ceil(len(ordered) * 0.95) - 1)]


def build_metrics(runs: list[RunRecord], evaluation: EvaluationReport | None) -> DashboardMetrics:
    completed = [run for run in runs if run.status == "completed"]
    queued = [run for run in runs if run.status == "queued"]
    running = [run for run in runs if run.status == "running"]
    awaiting = [run for run in runs if run.status == "awaiting_approval"]
    recovered = [run for run in runs if any(event.action == "recovered" for event in run.lease_history)]
    tools = [result for run in runs for result in run.tool_results]
    successful_tools = [result for result in tools if result.status in {"succeeded", "skipped"}]
    remote_tools = [result for result in tools if result.transport == "remote-http-fault-lab"]
    traces = [trace for run in runs for trace in run.traces if trace.duration_ms]
    citation_runs = [run for run in runs if run.sources]
    approval_runs = [run for run in runs if run.approval.required]
    model_calls = [call for run in runs for call in run.model_calls]
    live_calls = [call for call in model_calls if call.status == "succeeded"]
    fallbacks = [call for call in model_calls if call.status == "fallback"]

    today = datetime.now(timezone.utc).date()
    trend: list[dict] = []
    for offset in range(6, -1, -1):
        date = today - timedelta(days=offset)
        day_runs = [run for run in runs if run.created_at.date() == date]
        day_completed = [run for run in day_runs if run.status == "completed"]
        day_traces = [trace.duration_ms for run in day_runs for trace in run.traces if trace.duration_ms]
        trend.append(
            {
                "date": date.isoformat(),
                "success_rate": _percent(len(day_completed), len(day_runs)),
                "p95_ms": _p95(day_traces),
            }
        )

    return DashboardMetrics(
        total_runs=len(runs),
        completed_runs=len(completed),
        queued_runs=len(queued),
        running_runs=len(running),
        awaiting_approval=len(awaiting),
        recovered_runs=len(recovered),
        success_rate=_percent(len(completed), len(runs)),
        tool_success_rate=_percent(len(successful_tools), len(tools)),
        approval_rate=_percent(len(approval_runs), len(runs)),
        citation_coverage=_percent(len(citation_runs), len(runs)),
        p95_latency_ms=_p95([trace.duration_ms for trace in traces]),
        model_live_rate=_percent(len(live_calls), len(model_calls)),
        model_fallback_rate=_percent(len(fallbacks), len(model_calls)),
        average_model_latency_ms=(
            round(sum(call.latency_ms for call in model_calls) / len(model_calls)) if model_calls else 0
        ),
        remote_tool_rate=_percent(len(remote_tools), len(tools)),
        unsafe_action_rate=evaluation.unsafe_action_rate if evaluation else 0.0,
        evaluation_score=evaluation.score if evaluation else 0.0,
        trend=trend,
    )
