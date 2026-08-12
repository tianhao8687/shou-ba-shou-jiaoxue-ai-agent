from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.retrieval import (
    FeatureHashingProvider,
    HybridMemoryIndex,
    KnowledgeChunk,
    create_retriever,
)
from app.retrieval_config import load_retrieval_config


ROOT = Path(__file__).resolve().parents[2]


def test_retrieval_dataset_has_210_hashed_queries_across_required_categories() -> None:
    base = ROOT / "data" / "retrieval"
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["total_query_count"] == 210
    assert set(manifest["categories"]) == {
        "exact_identifier",
        "error_code",
        "natural_language_paraphrase",
        "synonym",
        "misleading_keyword",
        "multi_symptom",
        "weak_lexical_overlap",
    }
    assert manifest["holdout_frozen"] is True
    assert manifest["splits"]["calibration"]["tuning_allowed"] is True
    assert manifest["splits"]["holdout"]["tuning_allowed"] is False
    for metadata in manifest["splits"].values():
        payload = (base / metadata["path"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == metadata["sha256"]
        assert len(json.loads(payload)["queries"]) == metadata["query_count"] == 105


def test_runtime_records_external_retrieval_config_hash() -> None:
    config = load_retrieval_config()
    retriever = create_retriever(ROOT / "data", "memory", "sqlite:///unused.db")

    assert retriever.config == config
    assert retriever.config_hash == config.config_hash
    assert len(retriever.config_hash) == 64
    assert config.chunk.target_characters == 960
    assert config.chunk.overlap_characters == 0


def test_disabled_dense_channel_with_no_lexical_match_returns_empty_set() -> None:
    index = HybridMemoryIndex(
        [
            KnowledgeChunk(
                chunk_id="RB-TEST#C01",
                doc_id="RB-TEST",
                title="缓存刷新",
                section="处置",
                content="缓存版本落后时刷新指定租户缓存。",
                uri="knowledge://test#RB-TEST#C01",
            )
        ],
        FeatureHashingProvider(),
    )

    assert index.search("completely-unseen-kubernetes-backlog", limit=3) == []


def test_parameter_sweep_records_dataset_metric_range_selection_and_sensitivity() -> None:
    evidence = json.loads(
        (
            ROOT
            / "data"
            / "retrieval"
            / "calibration"
            / "sweep-results.json"
        ).read_text(encoding="utf-8")
    )
    assert evidence["holdout_accessed"] is False
    assert evidence["query_count"] == 105
    assert len(evidence["dataset_hash"]) == 64
    assert evidence["selected_config"] == load_retrieval_config().model_dump(
        mode="json", by_alias=True
    )
    assert evidence["selected_config_hash"] == load_retrieval_config().config_hash
    assert {
        "chunk.target_characters",
        "chunk.overlap_characters",
        "rrf.pool",
        "rrf.constant",
        "semantic.lexical_threshold",
        "semantic.low_lexical_dense_weight",
        "semantic.high_lexical_dense_weight",
    } == set(evidence["sweeps"])
    for result in evidence["sweeps"].values():
        assert result["metric"]
        assert result["search_range"]
        assert result["selected_value"] in result["search_range"]
        assert result["sensitivity"] >= 0
        assert result["effect"] in {"low", "material"}
