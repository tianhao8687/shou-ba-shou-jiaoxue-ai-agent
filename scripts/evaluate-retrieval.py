from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from math import ceil, log2
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.retrieval import OpenAIEmbeddingProvider, create_retriever  # noqa: E402
from app.retrieval_config import load_retrieval_config  # noqa: E402


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(payload)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, ceil(len(ordered) * percentile_value) - 1)
    return round(ordered[index], 3)


def deep_size(value: Any, seen: set[int] | None = None) -> int:
    visited = seen or set()
    identity = id(value)
    if identity in visited:
        return 0
    visited.add(identity)
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        return size + sum(
            deep_size(key, visited) + deep_size(item, visited)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return size + sum(deep_size(item, visited) for item in value)
    return size


def memory_contract(retriever) -> dict[str, int | str]:
    index = retriever.index
    lexical = deep_size(index.token_counts) + deep_size(index.idf) + deep_size(
        index.lengths
    )
    dense = deep_size(index.embeddings)
    return {
        "measurement": "recursive-python-container-estimate",
        "bm25_bytes": lexical,
        "embedding_bytes": dense,
        "hybrid_bytes": lexical + dense,
    }


def chunk_ranking(retriever, query: str, mode: str) -> list[str]:
    index = retriever.index
    if mode == "bm25":
        return index._bm25(query)
    if mode == "embedding":
        return index._dense(query)
    if mode == "hybrid":
        return [hit.chunk_id for hit in index.search(query, limit=256)]
    raise ValueError(f"unknown retrieval mode: {mode}")


def unique_docs(retriever, chunk_ids: list[str]) -> list[str]:
    ranking = []
    for chunk_id in chunk_ids:
        doc_id = retriever.index.by_id[chunk_id].doc_id
        if doc_id not in ranking:
            ranking.append(doc_id)
    return ranking


def evaluate_mode(
    retriever, cases: list[dict[str, Any]], mode: str
) -> dict[str, Any]:
    rows = []
    latencies = []
    reciprocal_ranks = []
    ndcg_values = []
    recall3_values = []
    recall5_values = []
    for case in cases:
        started = time.perf_counter()
        ranking = unique_docs(retriever, chunk_ranking(retriever, case["query"], mode))
        latency = (time.perf_counter() - started) * 1000
        latencies.append(latency)
        expected = list(case["expected_docs"])
        ranks = [ranking.index(doc_id) + 1 for doc_id in expected if doc_id in ranking]
        reciprocal_ranks.append(1 / min(ranks) if ranks else 0.0)
        recall3 = sum(doc_id in ranking[:3] for doc_id in expected) / len(expected)
        recall5 = sum(doc_id in ranking[:5] for doc_id in expected) / len(expected)
        recall3_values.append(recall3)
        recall5_values.append(recall5)
        dcg = sum(1 / log2(rank + 1) for rank in ranks)
        ideal = sum(1 / log2(rank + 1) for rank in range(1, len(expected) + 1))
        ndcg_values.append(dcg / ideal if ideal else 0.0)
        rows.append(
            {
                "id": case["id"],
                "category": case["category"],
                "expected_docs": expected,
                "top5": ranking[:5],
                "first_relevant_rank": min(ranks) if ranks else None,
                "recall_at_3": round(recall3, 4),
                "recall_at_5": round(recall5, 4),
                "latency_ms": round(latency, 3),
            }
        )
    category_breakdown = {}
    for category in sorted({row["category"] for row in rows}):
        selected = [row for row in rows if row["category"] == category]
        category_breakdown[category] = {
            "query_count": len(selected),
            "recall_at_3": round(
                statistics.mean(row["recall_at_3"] for row in selected), 4
            ),
            "recall_at_5": round(
                statistics.mean(row["recall_at_5"] for row in selected), 4
            ),
        }
    return {
        "mode": mode,
        "query_count": len(rows),
        "recall_at_3": round(statistics.mean(recall3_values), 4),
        "recall_at_5": round(statistics.mean(recall5_values), 4),
        "mrr": round(statistics.mean(reciprocal_ranks), 4),
        "ndcg": round(statistics.mean(ndcg_values), 4),
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 3),
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
            "max": round(max(latencies), 3),
        },
        "category_breakdown": category_breakdown,
        "results": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare BM25, Qwen embedding and hybrid retrieval on a frozen split."
    )
    parser.add_argument("--endpoint", default="http://127.0.0.1:8093/v1")
    parser.add_argument(
        "--model", default="OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov"
    )
    parser.add_argument("--api-key", default="harbor-local-embedding-token")
    parser.add_argument(
        "--split", choices=("calibration", "holdout"), default="calibration"
    )
    parser.add_argument("--final-holdout", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "data"
        / "retrieval"
        / "evidence"
        / "calibration-comparison.json",
    )
    args = parser.parse_args()
    if args.split == "holdout" and not args.final_holdout:
        print("frozen holdout requires --final-holdout", file=sys.stderr)
        return 2

    manifest_path = ROOT / "data" / "retrieval" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    split = manifest["splits"][args.split]
    dataset_path = manifest_path.parent / split["path"]
    raw = dataset_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != split["sha256"]:
        raise RuntimeError("retrieval dataset hash mismatch")
    cases = json.loads(raw)["queries"]
    config = load_retrieval_config()
    retriever = create_retriever(
        ROOT / "data",
        "memory",
        "sqlite:///unused.db",
        "openai-compatible",
        args.endpoint,
        args.model,
        args.api_key,
        retrieval_config=config,
    )
    retriever.index.embedding_provider.embed_queries(
        [case["query"] for case in cases]
    )
    modes = {
        mode: evaluate_mode(retriever, cases, mode)
        for mode in ("bm25", "embedding", "hybrid")
    }
    bm25 = modes["bm25"]
    embedding = modes["embedding"]
    hybrid = modes["hybrid"]
    stable_hybrid_advantage = (
        hybrid["recall_at_3"] >= max(bm25["recall_at_3"], embedding["recall_at_3"])
        and hybrid["mrr"] > max(bm25["mrr"], embedding["mrr"])
    )
    evidence = {
        "schema": "harbor-retrieval-evidence/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "dataset_version": manifest["dataset_version"],
        "dataset_split": args.split,
        "dataset_hash": split["sha256"],
        "query_count": len(cases),
        "model": args.model,
        "embedding_provider": retriever.index.embedding_provider.name,
        "embedding_dimensions": getattr(
            retriever.index.embedding_provider, "dimensions", None
        ),
        "retrieval_config": config.model_dump(mode="json", by_alias=True),
        "retrieval_config_hash": config.config_hash,
        "metrics": modes,
        "memory_usage": memory_contract(retriever),
        "hybrid_lift_over_bm25": {
            metric: round(hybrid[metric] - bm25[metric], 4)
            for metric in ("recall_at_3", "recall_at_5", "mrr", "ndcg")
        },
        "semantic_lift_over_bm25": {
            metric: round(embedding[metric] - bm25[metric], 4)
            for metric in ("recall_at_3", "recall_at_5", "mrr", "ndcg")
        },
        "stable_hybrid_advantage": stable_hybrid_advantage,
        "embedding_runtime_recommendation": (
            "default-enabled-on-this-split"
            if stable_hybrid_advantage
            else "optional-no-stable-hybrid-advantage"
        ),
    }
    atomic_json(args.output.resolve(), evidence)
    print(
        json.dumps(
            {
                "dataset_split": args.split,
                "query_count": len(cases),
                "bm25": {key: bm25[key] for key in ("recall_at_3", "recall_at_5", "mrr", "ndcg")},
                "embedding": {key: embedding[key] for key in ("recall_at_3", "recall_at_5", "mrr", "ndcg")},
                "hybrid": {key: hybrid[key] for key in ("recall_at_3", "recall_at_5", "mrr", "ndcg")},
                "recommendation": evidence["embedding_runtime_recommendation"],
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
