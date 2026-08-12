from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.evaluation import EvaluationService
from app.evaluation_baselines import RuleBaseline, WorkflowBaseline, evaluate_baseline
from app.retrieval import create_retriever


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "data" / "evaluation"


def test_dataset_has_disjoint_hashed_splits_and_required_taxonomy() -> None:
    manifest = json.loads((DATASET / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["total_case_count"] == 120
    assert set(manifest["splits"]) == {"calibration", "development", "holdout"}
    assert {item["case_count"] for item in manifest["splits"].values()} == {
        30,
        40,
        50,
    }
    required = {
        "latency",
        "timeout",
        "dependency_failure",
        "authentication_failure",
        "connection_pool_exhaustion",
        "queue_backlog",
        "stale_cache",
        "partial_outage",
        "configuration_drift",
        "bad_deployment",
        "resource_pressure",
        "misleading_symptoms",
        "multiple_plausible_causes",
        "insufficient_evidence",
    }
    assert set(manifest["taxonomy"]) == required

    seen: set[str] = set()
    for split, metadata in manifest["splits"].items():
        payload_bytes = (DATASET / metadata["path"]).read_bytes()
        assert hashlib.sha256(payload_bytes).hexdigest() == metadata["sha256"]
        payload = json.loads(payload_bytes)
        ids = {case["id"] for case in payload["cases"]}
        assert len(ids) == metadata["case_count"]
        assert not seen.intersection(ids)
        assert all(case["split"] == split for case in payload["cases"])
        seen.update(ids)


def test_frozen_holdout_requires_explicit_final_evaluation_unlock() -> None:
    retriever = create_retriever(
        ROOT / "data", "memory", "sqlite:///unused.db", "lexical-feature-baseline"
    )
    evaluator = EvaluationService(
        DATASET / "manifest.json",
        retriever,
        "dataset-isolation-test-secret-long-enough",
    )

    with pytest.raises(ValueError, match="explicit final-evaluation unlock"):
        evaluator.run(tenant_id="dataset-test", split="holdout", case_limit=1)


def test_rule_and_workflow_baselines_publish_required_metrics() -> None:
    cases = json.loads(
        (DATASET / "calibration" / "cases.json").read_text(encoding="utf-8")
    )["cases"]
    for baseline in (RuleBaseline(), WorkflowBaseline()):
        result = evaluate_baseline(baseline, cases)
        assert result["case_count"] == 30
        assert {
            "diagnosis_accuracy",
            "recovery_success_rate",
            "unsafe_action_rate",
            "unsupported_claim_rate",
            "correct_tool_selection",
            "handoff_quality",
            "unnecessary_action_rate",
            "normalized_mttr_ms",
            "average_tool_calls",
            "human_intervention_rate",
        } <= set(result)
