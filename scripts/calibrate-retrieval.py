from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from math import log2
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.retrieval import (  # noqa: E402
    HybridMemoryIndex,
    OpenAIEmbeddingProvider,
    Retriever,
    chunk_document,
    load_documents,
)
from app.retrieval_config import RetrievalConfig, load_retrieval_config  # noqa: E402


SEARCH_RANGES: dict[str, list[float | int]] = {
    "chunk.target_characters": [480, 720, 960],
    "chunk.overlap_characters": [0, 120, 240],
    "rrf.pool": [8, 16, 24, 48, 96],
    "rrf.constant": [10, 30, 60, 90],
    "semantic.lexical_threshold": [0.2, 0.35, 0.5, 0.65, 0.8],
    "semantic.low_lexical_dense_weight": [0.2, 0.4, 0.6, 0.8, 1.0],
    "semantic.high_lexical_dense_weight": [0.0, 0.1, 0.3, 0.5, 0.7, 0.9],
}


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


def set_parameter(
    config: RetrievalConfig, path: str, value: float | int
) -> RetrievalConfig:
    payload = config.model_dump(mode="json", by_alias=True)
    group, field = path.split(".", 1)
    payload[group][field] = value
    return RetrievalConfig.model_validate(payload)


def build_retriever(
    config: RetrievalConfig,
    documents,
    provider: OpenAIEmbeddingProvider,
) -> Retriever:
    chunks = [
        chunk
        for document in documents
        for chunk in chunk_document(document, config)
    ]
    return Retriever(
        documents,
        chunks,
        HybridMemoryIndex(chunks, provider, config),
        config,
    )


def metrics(retriever: Retriever, cases: list[dict[str, Any]]) -> dict[str, float]:
    recall3 = []
    recall5 = []
    reciprocal = []
    ndcg = []
    for case in cases:
        hits = retriever.index.search(case["query"], limit=256)
        ranking = []
        for hit in hits:
            if hit.doc_id not in ranking:
                ranking.append(hit.doc_id)
        expected = list(case["expected_docs"])
        ranks = [ranking.index(doc) + 1 for doc in expected if doc in ranking]
        recall3.append(sum(doc in ranking[:3] for doc in expected) / len(expected))
        recall5.append(sum(doc in ranking[:5] for doc in expected) / len(expected))
        reciprocal.append(1 / min(ranks) if ranks else 0.0)
        dcg = sum(1 / log2(rank + 1) for rank in ranks)
        ideal = sum(1 / log2(rank + 1) for rank in range(1, len(expected) + 1))
        ndcg.append(dcg / ideal if ideal else 0.0)
    result = {
        "recall_at_3": round(statistics.mean(recall3), 6),
        "recall_at_5": round(statistics.mean(recall5), 6),
        "mrr": round(statistics.mean(reciprocal), 6),
        "ndcg": round(statistics.mean(ndcg), 6),
    }
    result["objective"] = round(
        result["mrr"] * 0.4
        + result["recall_at_3"] * 0.3
        + result["recall_at_5"] * 0.2
        + result["ndcg"] * 0.1,
        6,
    )
    return result


def simplest(path: str, values: list[float | int]) -> float | int:
    if path == "chunk.target_characters":
        return max(values)
    if path == "semantic.lexical_threshold":
        return min(values, key=lambda item: (abs(float(item) - 0.5), float(item)))
    if path == "rrf.constant":
        return min(values, key=lambda item: (abs(float(item) - 60), float(item)))
    return min(values)


def choose(path: str, rows: list[dict[str, Any]]) -> float | int:
    best = max(row["metrics"]["objective"] for row in rows)
    practically_tied = [
        row["value"]
        for row in rows
        if best - row["metrics"]["objective"] <= 0.002
    ]
    return simplest(path, practically_tied)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:8093/v1")
    parser.add_argument(
        "--model", default="OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov"
    )
    parser.add_argument("--api-key", default="harbor-local-embedding-token")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT
        / "data"
        / "retrieval"
        / "calibration"
        / "sweep-results.json",
    )
    args = parser.parse_args()
    manifest_path = ROOT / "data" / "retrieval" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata = manifest["splits"]["calibration"]
    raw = (manifest_path.parent / metadata["path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
        raise RuntimeError("calibration dataset hash mismatch")
    cases = json.loads(raw)["queries"]
    documents = load_documents(ROOT / "data" / "knowledge")
    provider = OpenAIEmbeddingProvider(
        args.endpoint, args.model, args.api_key, timeout_seconds=120
    )
    provider.embed_queries([case["query"] for case in cases])
    selected = load_retrieval_config()
    baseline = metrics(build_retriever(selected, documents, provider), cases)
    sweeps = {}
    for path, values in SEARCH_RANGES.items():
        rows = []
        for value in values:
            candidate = set_parameter(selected, path, value)
            score = metrics(build_retriever(candidate, documents, provider), cases)
            rows.append({"value": value, "metrics": score})
        chosen = choose(path, rows)
        objective_values = [row["metrics"]["objective"] for row in rows]
        sensitivity = round(max(objective_values) - min(objective_values), 6)
        sweeps[path] = {
            "metric": "0.4*MRR + 0.3*Recall@3 + 0.2*Recall@5 + 0.1*nDCG",
            "search_range": values,
            "selected_value": chosen,
            "sensitivity": sensitivity,
            "effect": "low" if sensitivity < 0.005 else "material",
            "results": rows,
        }
        selected = set_parameter(selected, path, chosen)
    selected_metrics = metrics(build_retriever(selected, documents, provider), cases)
    evidence = {
        "schema": "harbor-retrieval-calibration/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit(),
        "calibration_dataset": metadata["path"],
        "dataset_version": manifest["dataset_version"],
        "dataset_hash": metadata["sha256"],
        "query_count": len(cases),
        "model": args.model,
        "baseline_config": load_retrieval_config().model_dump(
            mode="json", by_alias=True
        ),
        "baseline_config_hash": load_retrieval_config().config_hash,
        "baseline_metrics": baseline,
        "sweeps": sweeps,
        "selected_config": selected.model_dump(mode="json", by_alias=True),
        "selected_config_hash": selected.config_hash,
        "selected_metrics": selected_metrics,
        "holdout_accessed": False,
    }
    atomic_json(args.output.resolve(), evidence)
    print(
        json.dumps(
            {
                "baseline": baseline,
                "selected": selected_metrics,
                "selected_config": evidence["selected_config"],
                "low_effect_parameters": [
                    path for path, result in sweeps.items() if result["effect"] == "low"
                ],
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
