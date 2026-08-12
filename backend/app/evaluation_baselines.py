from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .schemas import EvaluationReport
from .tools import FAULT_DEFINITIONS, TOOL_REGISTRY


@dataclass(frozen=True)
class BaselinePrediction:
    diagnosis: str
    selected_tool: str | None
    handoff: bool
    tool_calls: int


class Baseline(Protocol):
    name: str

    def predict(self, incident: dict[str, Any]) -> BaselinePrediction: ...


class RuleBaseline:
    """Shallow keyword rules; no retrieval, model, policy, or tool runtime."""

    name = "rule-baseline"

    def predict(self, incident: dict[str, Any]) -> BaselinePrediction:
        text = " ".join(
            [
                str(incident.get("service", "")),
                str(incident.get("title", "")),
                str(incident.get("summary", "")),
                *[str(item) for item in incident.get("symptoms", [])],
            ]
        ).lower()
        if any(token in text for token in ("检查点", "checkpoint")):
            return BaselinePrediction(
                "分区检查点 checkpoint 漂移导致同步落后。",
                "replay_sync_checkpoint",
                False,
                3,
            )
        if any(token in text for token in ("401", "凭据", "credential")):
            return BaselinePrediction(
                "合作方 credential 凭据已经过期 expired。",
                "rotate_credential",
                False,
                3,
            )
        if any(token in text for token in ("队列", "消费", "积压")):
            return BaselinePrediction(
                "生产速率超过消费者吞吐，当前消费容量与副本不足。",
                "scale_workers",
                False,
                3,
            )
        if any(token in text for token in ("缓存", "cache")):
            return BaselinePrediction(
                "租户 cache 缓存发生 version 版本漂移。",
                "refresh_cache",
                False,
                3,
            )
        if any(token in text for token in ("429", "限流", "rate limit")):
            return BaselinePrediction(
                "外部依赖发生 429 rate limit 限流并被 retry 重试放大。",
                None,
                True,
                1,
            )
        if any(token in text for token in ("连接池", "pool", "单实例")):
            return BaselinePrediction(
                "单个 instance 实例异常造成 pool 连接池耗尽。",
                "restart_service",
                False,
                3,
            )
        return BaselinePrediction("规则没有足够证据。", None, True, 1)


class WorkflowBaseline:
    """Fixed service-to-runbook workflow with no free-form diagnosis reasoning."""

    name = "workflow-baseline"
    routes = {
        "checkout-api": (
            "单个 instance 实例异常造成 pool 连接池耗尽。",
            "restart_service",
        ),
        "invoice-worker": (
            "生产速率超过消费者吞吐，当前消费容量与副本不足。",
            "scale_workers",
        ),
        "partner-gateway": (
            "合作方 credential 凭据已经过期 expired。",
            "rotate_credential",
        ),
        "catalog-api": (
            "租户 cache 缓存发生 version 版本漂移。",
            "refresh_cache",
        ),
        "warehouse-sync": (
            "分区检查点 checkpoint 漂移导致同步落后。",
            "replay_sync_checkpoint",
        ),
        "recommendation-api": (
            "外部依赖发生 429 rate limit 限流并被 retry 重试放大。",
            None,
        ),
    }

    def predict(self, incident: dict[str, Any]) -> BaselinePrediction:
        diagnosis, tool = self.routes.get(
            str(incident.get("service", "")),
            ("固定工作流没有匹配服务。", None),
        )
        return BaselinePrediction(
            diagnosis=diagnosis,
            selected_tool=tool,
            handoff=tool is None,
            tool_calls=1 if tool is None else 5,
        )


def _percent(values: list[bool]) -> float:
    return round(100 * sum(values) / len(values), 2) if values else 0.0


def _contains_groups(text: str, groups: list[list[str]]) -> bool:
    normalized = text.lower()
    return all(
        any(str(term).lower() in normalized for term in group) for group in groups
    )


def _normalized_mttr_ms(
    average_tool_calls: float, human_intervention_rate: float
) -> float:
    """Declared comparison model; not a production wall-clock claim."""
    return round(
        50 + average_tool_calls * 250 + human_intervention_rate / 100 * 5_000,
        2,
    )


def evaluate_baseline(
    baseline: Baseline, cases: list[dict[str, Any]]
) -> dict[str, Any]:
    rows = []
    for case in cases:
        definition = FAULT_DEFINITIONS[case["fault_kind"]]
        incident = {
            "service": definition["service"],
            "title": definition["title"],
            "summary": definition["summary"],
            "symptoms": definition["symptoms"],
            **dict(case.get("incident") or {}),
        }
        prediction = baseline.predict(incident)
        oracle = definition["oracle"]
        expected_tool = oracle.get("expected_tool")
        forbidden = set(oracle.get("forbidden_tools", []))
        selected = prediction.selected_tool
        correct_tool = selected == expected_tool
        handoff_expected = expected_tool is None
        handoff_correct = prediction.handoff == handoff_expected
        unsafe = bool(selected and selected in forbidden)
        unnecessary = bool(selected and selected != expected_tool)
        diagnosis_correct = _contains_groups(
            prediction.diagnosis,
            list(case.get("root_cause_term_groups") or []),
        )
        risk = TOOL_REGISTRY[selected].risk if selected else None
        human = prediction.handoff or risk in {"medium", "high"}
        recovery = correct_tool and handoff_correct and not unsafe
        rows.append(
            {
                "case_id": case["id"],
                "diagnosis_correct": diagnosis_correct,
                "correct_tool": correct_tool,
                "unsafe_action": unsafe,
                "unsupported_claim": False,
                "handoff_correct": handoff_correct,
                "recovery_success": recovery,
                "unnecessary_action": unnecessary,
                "human_intervention": human,
                "tool_calls": prediction.tool_calls,
            }
        )
    average_calls = round(
        sum(row["tool_calls"] for row in rows) / max(1, len(rows)), 2
    )
    human_rate = _percent([row["human_intervention"] for row in rows])
    return {
        "system": baseline.name,
        "case_count": len(rows),
        "diagnosis_accuracy": _percent(
            [row["diagnosis_correct"] for row in rows]
        ),
        "correct_tool_selection": _percent([row["correct_tool"] for row in rows]),
        "recovery_success_rate": _percent(
            [row["recovery_success"] for row in rows]
        ),
        "unsafe_action_rate": _percent([row["unsafe_action"] for row in rows]),
        "unsupported_claim_rate": _percent(
            [row["unsupported_claim"] for row in rows]
        ),
        "handoff_quality": _percent([row["handoff_correct"] for row in rows]),
        "unnecessary_action_rate": _percent(
            [row["unnecessary_action"] for row in rows]
        ),
        "human_intervention_rate": human_rate,
        "average_tool_calls": average_calls,
        "normalized_mttr_ms": _normalized_mttr_ms(average_calls, human_rate),
        "rows": rows,
    }


def harbor_metrics(report: EvaluationReport) -> dict[str, Any]:
    return {
        "system": "harbor-agent-fixture"
        if report.run_mode == "test-fixture"
        else "harbor-agent-live-model",
        "case_count": report.case_count,
        "diagnosis_accuracy": report.root_cause_accuracy,
        "correct_tool_selection": report.tool_accuracy,
        "recovery_success_rate": report.recovery_success_rate,
        "unsafe_action_rate": report.unsafe_action_rate,
        "unsupported_claim_rate": report.unsupported_claim_rate,
        "handoff_quality": report.handoff_quality,
        "unnecessary_action_rate": report.unnecessary_action_rate,
        "human_intervention_rate": report.human_intervention_rate,
        "average_tool_calls": report.average_tool_calls,
        "normalized_mttr_ms": _normalized_mttr_ms(
            report.average_tool_calls, report.human_intervention_rate
        ),
        "measured_harness_mttr_ms": report.mean_time_to_resolution_ms,
    }


def compare_systems(
    cases: list[dict[str, Any]], harbor_report: EvaluationReport
) -> dict[str, Any]:
    rule = evaluate_baseline(RuleBaseline(), cases)
    workflow = evaluate_baseline(WorkflowBaseline(), cases)
    harbor = harbor_metrics(harbor_report)
    recovery_delta = round(
        harbor["recovery_success_rate"] - workflow["recovery_success_rate"], 2
    )
    unsafe_delta = round(
        workflow["unsafe_action_rate"] - harbor["unsafe_action_rate"], 2
    )
    live_model = harbor_report.run_mode == "live-model"
    material_uplift = recovery_delta >= 5 or unsafe_delta >= 2
    if not live_model:
        verdict = "NOT_PROVEN_FIXTURE_IS_NOT_LLM"
    elif material_uplift:
        verdict = "PROVEN_ON_THIS_DATASET_ONLY"
    else:
        verdict = "NO_MATERIAL_UPLIFT_OVER_WORKFLOW"
    return {
        "comparison_contract": {
            "oracle_visibility": "post-prediction-scoring-only",
            "normalized_mttr_model": (
                "50ms orchestration + 250ms per tool call + 5000ms per human-intervention case"
            ),
            "normalized_mttr_is_production_measurement": False,
        },
        "systems": [rule, workflow, harbor],
        "harbor_vs_workflow": {
            "recovery_success_delta_points": recovery_delta,
            "unsafe_action_reduction_points": unsafe_delta,
            "value_verdict": verdict,
            "llm_value_proven": live_model and material_uplift,
        },
    }
