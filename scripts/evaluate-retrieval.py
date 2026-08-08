from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from math import ceil
from pathlib import Path
import statistics
import sys
import time
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.retrieval import create_retriever


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, ceil(len(ordered) * percentile_value) - 1)
    return round(ordered[index], 2)


def unique_doc_ranking(retriever, query: str) -> list[str]:
    chunks = retriever.index.search(query, limit=64)
    ranking: list[str] = []
    for chunk in chunks:
        if chunk.doc_id not in ranking:
            ranking.append(chunk.doc_id)
    return ranking


def evaluate(retriever, cases: list[dict[str, str]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    reciprocal_ranks: list[float] = []
    for case in cases:
        started = time.perf_counter()
        ranking = unique_doc_ranking(retriever, case["query"])
        latency_ms = (time.perf_counter() - started) * 1000
        latencies.append(latency_ms)
        try:
            rank = ranking.index(case["expected_doc"]) + 1
        except ValueError:
            rank = None
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
        rows.append(
            {
                **case,
                "rank": rank,
                "top3": ranking[:3],
                "hit_at_1": rank == 1,
                "hit_at_3": bool(rank and rank <= 3),
                "latency_ms": round(latency_ms, 2),
            }
        )
    total = len(rows)
    return {
        "backend": retriever.backend_name,
        "quality": retriever.vector_quality,
        "cases": total,
        "recall_at_1": round(
            sum(row["hit_at_1"] for row in rows) / total,
            4,
        ),
        "recall_at_3": round(
            sum(row["hit_at_3"] for row in rows) / total,
            4,
        ),
        "mrr": round(statistics.mean(reciprocal_ranks), 4),
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 2),
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "max": round(max(latencies), 2),
        },
        "results": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate lexical and real semantic Harbor retrieval"
    )
    parser.add_argument(
        "--endpoint",
        default="http://127.0.0.1:8093/v1",
    )
    parser.add_argument(
        "--model",
        default="OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov",
    )
    parser.add_argument(
        "--api-key",
        default="harbor-local-embedding-token",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "docs" / "retrieval-evidence-v3.1.json",
    )
    args = parser.parse_args()
    calibration_cases = json.loads(
        (PROJECT_ROOT / "data" / "retrieval_eval_v3_1.json").read_text(
            encoding="utf-8"
        )
    )
    holdout_cases = json.loads(
        (
            PROJECT_ROOT
            / "data"
            / "retrieval_eval_holdout_v3_1.json"
        ).read_text(encoding="utf-8")
    )
    lexical = create_retriever(
        PROJECT_ROOT / "data",
        "memory",
        "sqlite:///unused.db",
    )
    semantic = create_retriever(
        PROJECT_ROOT / "data",
        "memory",
        "sqlite:///unused.db",
        "openai-compatible",
        args.endpoint,
        args.model,
        args.api_key,
    )
    report = {
        "schema": "harbor-retrieval-eval/v3.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "datasets": {
            "calibration": "data/retrieval_eval_v3_1.json",
            "holdout": "data/retrieval_eval_holdout_v3_1.json",
        },
        "scope": (
            "15 calibration plus 15 holdout AIOps paraphrases; direct learned "
            "retrieval without metadata routing or mandatory policy pinning"
        ),
        "calibration": {
            "lexical_feature_baseline": evaluate(
                lexical,
                calibration_cases,
            ),
            "qwen3_semantic": evaluate(
                semantic,
                calibration_cases,
            ),
        },
        "holdout": {
            "lexical_feature_baseline": evaluate(
                lexical,
                holdout_cases,
            ),
            "qwen3_semantic": evaluate(
                semantic,
                holdout_cases,
            ),
        },
    }
    semantic_metrics = report["holdout"]["qwen3_semantic"]
    lexical_metrics = report["holdout"]["lexical_feature_baseline"]
    report["holdout"]["semantic_lift"] = {
        "recall_at_1": round(
            semantic_metrics["recall_at_1"]
            - lexical_metrics["recall_at_1"],
            4,
        ),
        "recall_at_3": round(
            semantic_metrics["recall_at_3"]
            - lexical_metrics["recall_at_3"],
            4,
        ),
        "mrr": round(
            semantic_metrics["mrr"] - lexical_metrics["mrr"],
            4,
        ),
    }
    report["gate"] = {
        "evaluated_split": "holdout",
        "recall_at_3_minimum": 0.90,
        "semantic_mrr_must_not_regress": True,
        "passed": (
            semantic_metrics["recall_at_3"] >= 0.90
            and semantic_metrics["mrr"] >= lexical_metrics["mrr"]
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["gate"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
