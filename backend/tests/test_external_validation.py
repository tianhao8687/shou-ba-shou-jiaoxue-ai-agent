from __future__ import annotations

import copy
import hashlib
import csv
from datetime import datetime, timedelta
import io
import json
import math
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.external_validation import (
    ExternalDataFetcher,
    ExternalSourceManifest,
    ExternalValidationError,
    _MultinomialLogClassifier,
    _classification_metrics,
    _log_tokens,
    _parse_timestamp,
    atomic_write_report,
    load_external_report,
    run_external_validation,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REVISION = "a" * 40


def _manifest(payload: bytes, *, sha256: str | None = None) -> ExternalSourceManifest:
    sources = []
    for index in range(3):
        sources.append(
            {
                "id": f"source-{index}",
                "publisher": "test publisher",
                "title": "test source",
                "repository_url": "https://example.invalid/source",
                "revision": REVISION,
                "data_class": "test",
                "independence": "independent test fixture",
                "license": {
                    "name": "test",
                    "url": "https://example.invalid/license",
                    "raw_redistribution": "not-committed",
                },
                "files": [
                    {
                        "id": f"file-{index}",
                        "url": f"https://raw.githubusercontent.com/test/repo/{REVISION}/file-{index}.bin",
                        "local_name": f"source-{index}/file.bin",
                        "sha256": sha256 or hashlib.sha256(payload).hexdigest(),
                        "bytes": len(payload),
                        "role": "holdout",
                        "dataset_path": f"file-{index}.bin",
                    }
                ],
            }
        )
    return ExternalSourceManifest.model_validate(
        {
            "schema": "harbor-external-source-manifest/v1",
            "suite_version": "test-v1",
            "frozen_at": "2026-08-10",
            "partition_seed": "test-seed",
            "sources": sources,
            "gates": {},
        }
    )


def test_fetcher_verifies_bytes_then_replays_offline(tmp_path: Path) -> None:
    payload = b"third-party-bytes"
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=payload, request=request)

    fetcher = ExternalDataFetcher(
        _manifest(payload),
        tmp_path,
        transport=httpx.MockTransport(handler),
    )
    paths, receipts = fetcher.fetch_all()
    assert calls == 3
    assert all(path.read_bytes() == payload for path in paths.values())
    assert all(not receipt.cache_hit for receipt in receipts)

    _, offline_receipts = fetcher.fetch_all(offline=True)
    assert calls == 3
    assert all(receipt.cache_hit for receipt in offline_receipts)


def test_fetcher_fails_closed_when_source_bytes_drift(tmp_path: Path) -> None:
    expected = b"expected-source"
    changed = b"changed--source"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=changed, request=request)

    fetcher = ExternalDataFetcher(
        _manifest(expected),
        tmp_path,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ExternalValidationError, match="hash or size mismatch"):
        fetcher.fetch_all()
    assert not list(tmp_path.rglob("*.bin"))


def test_manifest_rejects_untrusted_locations_and_ambiguous_ids() -> None:
    base = _manifest(b"manifest-contract").model_dump(by_alias=True)

    invalid_url = copy.deepcopy(base)
    invalid_url["sources"][0]["files"][0]["url"] = "http://evil.example/file"
    with pytest.raises(ValidationError, match="allowlisted HTTPS host"):
        ExternalSourceManifest.model_validate(invalid_url)

    credential_url = copy.deepcopy(base)
    credential_url["sources"][0]["files"][0]["url"] = (
        f"https://user@raw.githubusercontent.com/test/repo/{REVISION}/file"
    )
    with pytest.raises(ValidationError, match="credentials or fragments"):
        ExternalSourceManifest.model_validate(credential_url)

    escaped_path = copy.deepcopy(base)
    escaped_path["sources"][0]["files"][0]["local_name"] = "../escape.bin"
    with pytest.raises(ValidationError, match="safe relative POSIX path"):
        ExternalSourceManifest.model_validate(escaped_path)

    empty_files = copy.deepcopy(base)
    empty_files["sources"][0]["files"] = []
    with pytest.raises(ValidationError, match="non-empty and unique"):
        ExternalSourceManifest.model_validate(empty_files)

    unpinned = copy.deepcopy(base)
    unpinned["sources"][0]["files"][0]["url"] = (
        "https://raw.githubusercontent.com/test/repo/main/file.bin"
    )
    with pytest.raises(ValidationError, match="pinned to the source revision"):
        ExternalSourceManifest.model_validate(unpinned)

    too_few = copy.deepcopy(base)
    too_few["sources"] = too_few["sources"][:2]
    with pytest.raises(ValidationError, match="at least three"):
        ExternalSourceManifest.model_validate(too_few)

    duplicate_source = copy.deepcopy(base)
    duplicate_source["sources"][1]["id"] = duplicate_source["sources"][0]["id"]
    with pytest.raises(ValidationError, match="source ids must be unique"):
        ExternalSourceManifest.model_validate(duplicate_source)

    duplicate_file = copy.deepcopy(base)
    duplicate_file["sources"][1]["files"][0]["id"] = (
        duplicate_file["sources"][0]["files"][0]["id"]
    )
    with pytest.raises(ValidationError, match="file ids must be globally unique"):
        ExternalSourceManifest.model_validate(duplicate_file)


def test_fetcher_rejects_missing_cache_redirects_and_oversized_responses(
    tmp_path: Path,
) -> None:
    payload = b"bounded"
    manifest = _manifest(payload)
    with pytest.raises(ExternalValidationError, match="offline cache missing"):
        ExternalDataFetcher(manifest, tmp_path / "offline").fetch_all(offline=True)

    def redirected(request: httpx.Request) -> httpx.Response:
        if request.url.host in {"raw.githubusercontent.com", "www.aiops.cn"}:
            return httpx.Response(
                302,
                headers={"location": "https://evil.example/drift"},
                request=request,
            )
        return httpx.Response(200, content=payload, request=request)

    with pytest.raises(ExternalValidationError, match="non-allowlisted host"):
        ExternalDataFetcher(
            manifest,
            tmp_path / "redirect",
            transport=httpx.MockTransport(redirected),
        ).fetch_all()

    def oversized_header(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=payload,
            headers={"content-length": str(len(payload) + 1)},
            request=request,
        )

    with pytest.raises(ExternalValidationError, match="oversized response"):
        ExternalDataFetcher(
            manifest,
            tmp_path / "header",
            transport=httpx.MockTransport(oversized_header),
        ).fetch_all()

    def oversized_body(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx.ByteStream(payload + b"!"),
            request=request,
        )

    with pytest.raises(ExternalValidationError, match="oversized response"):
        ExternalDataFetcher(
            manifest,
            tmp_path / "body",
            transport=httpx.MockTransport(oversized_body),
        ).fetch_all()


def test_evaluator_primitives_reject_leak_prone_or_invalid_inputs(tmp_path: Path) -> None:
    manifest = _manifest(b"source")
    with pytest.raises(ExternalValidationError, match="unknown source"):
        manifest.source("missing")
    assert load_external_report(tmp_path / "missing.json") is None

    with pytest.raises(ExternalValidationError, match="lengths differ"):
        _classification_metrics([True], [])

    classifier = _MultinomialLogClassifier()
    with pytest.raises(ExternalValidationError, match="empty Loghub fit"):
        classifier.fit([])
    with pytest.raises(ExternalValidationError, match="both classes"):
        classifier.fit([(["only-normal"], False)])

    parsed = _parse_timestamp("2025-01-01T00:00:00Z")
    assert parsed.tzinfo is None


def test_log_features_cannot_read_oracle_fields() -> None:
    row = {
        "Label": "SECRET-LABEL",
        "EventId": "SECRET-EVENT",
        "EventTemplate": "SECRET-TEMPLATE",
        "Level": "FATAL",
        "Component": "KERNEL",
        "Type": "RAS",
        "Content": "disk failure at 10.2.3.4 block 912345",
    }
    serialized = " ".join(_log_tokens(row))
    assert "secret" not in serialized
    assert "level_fatal" in serialized
    assert "numbertoken" in serialized


def test_tracked_external_evidence_is_valid_and_disclaims_production() -> None:
    report = load_external_report(PROJECT_ROOT / "data" / "external" / "evidence-v1.json")
    assert report is not None
    assert report.production_claim is False
    assert report.sources_verified == 3
    assert report.source_files_verified == 8
    assert report.aggregate["autonomous_taxonomy_coverage"] == 0


def _csv_bytes(rows: list[dict[str, object]], fields: list[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def _synthetic_external_payloads() -> tuple[dict[str, bytes], dict[str, str]]:
    bgl_rows: list[dict[str, object]] = []
    for line_id in range(1, 601):
        anomalous = line_id % 10 == 0
        bgl_rows.append(
            {
                "LineId": line_id,
                "Label": "KERNFAIL" if anomalous else "-",
                "Level": "FATAL" if anomalous else "INFO",
                "Component": "KERNEL",
                "Type": "RAS",
                "Content": (
                    "disk controller failed hard" if anomalous else "heartbeat status normal"
                ),
                "EventId": "E-FAIL" if anomalous else "E-OK",
                "EventTemplate": "oracle-only-template",
            }
        )
    payloads = {
        "loghub-bgl-2k-structured": _csv_bytes(
            bgl_rows,
            [
                "LineId",
                "Label",
                "Level",
                "Component",
                "Type",
                "Content",
                "EventId",
                "EventTemplate",
            ],
        )
    }

    series_paths = {
        "nab-cpu-calibration": "realAWSCloudwatch/cpu.csv",
        "nab-speed-calibration": "realTraffic/speed.csv",
        "nab-network-holdout": "realAWSCloudwatch/network.csv",
        "nab-machine-holdout": "realKnownCause/machine.csv",
    }
    windows: dict[str, list[list[str]]] = {}
    start = datetime(2025, 1, 1)
    for series_index, (file_id, dataset_path) in enumerate(series_paths.items()):
        rows = []
        for point in range(360):
            timestamp = start + timedelta(minutes=5 * point)
            value = 10 + math.sin(point / 12) + series_index
            if 120 <= point <= 126 or 240 <= point <= 246:
                value += 30
            rows.append({"timestamp": timestamp.isoformat(sep=" "), "value": value})
        payloads[file_id] = _csv_bytes(rows, ["timestamp", "value"])
        windows[dataset_path] = [
            [
                (start + timedelta(minutes=5 * 118)).isoformat(sep=" "),
                (start + timedelta(minutes=5 * 132)).isoformat(sep=" "),
            ],
            [
                (start + timedelta(minutes=5 * 238)).isoformat(sep=" "),
                (start + timedelta(minutes=5 * 252)).isoformat(sep=" "),
            ],
        ]
    payloads["nab-anomaly-windows"] = json.dumps(windows).encode()

    inputs = []
    truths = []
    for index in range(10):
        uuid = f"case-{index}"
        inputs.append(
            {
                "uuid": uuid,
                "Anomaly Description": "Anomaly occurred inside the supplied time window.",
            }
        )
        truths.append(
            {
                "uuid": uuid,
                "fault_type": "cpu stress" if index % 2 else "network loss",
                "instance": f"pod-{index}",
                "service": "checkoutservice",
                "source": "",
                "destination": "",
                "key_observations": [{"type": "metric"}, {"type": "log"}],
            }
        )
    payloads["aiops-2025-input"] = json.dumps(inputs).encode()
    payloads["aiops-2025-groundtruth"] = (
        "\n".join(json.dumps(truth) for truth in truths) + "\n"
    ).encode()
    return payloads, series_paths


def _synthetic_manifest(payloads: dict[str, bytes], series_paths: dict[str, str]) -> dict:
    def file_entry(file_id: str, role: str, dataset_path: str) -> dict:
        payload = payloads[file_id]
        return {
            "id": file_id,
            "url": f"https://raw.githubusercontent.com/test/repo/{REVISION}/{file_id}",
            "local_name": f"synthetic/{file_id}",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "role": role,
            "dataset_path": dataset_path,
        }

    def source(source_id: str, files: list[dict]) -> dict:
        return {
            "id": source_id,
            "publisher": "independent synthetic contract test",
            "title": source_id,
            "repository_url": "https://example.invalid/repository",
            "revision": REVISION,
            "data_class": "contract-test-only",
            "independence": "Exercises code paths; never used as external quality evidence.",
            "license": {
                "name": "test",
                "url": "https://example.invalid/license",
                "raw_redistribution": "not-committed",
            },
            "files": files,
        }

    nab_files = [
        file_entry(
            file_id,
            "calibration" if "calibration" in file_id else "holdout",
            dataset_path,
        )
        for file_id, dataset_path in series_paths.items()
    ]
    nab_files.append(
        file_entry("nab-anomaly-windows", "scoring-oracle", "labels/windows.json")
    )
    return {
        "schema": "harbor-external-source-manifest/v1",
        "suite_version": "synthetic-contract-v1",
        "frozen_at": "2026-08-10",
        "partition_seed": "synthetic-contract-seed",
        "sources": [
            source(
                "loghub-bgl-2k",
                [
                    file_entry(
                        "loghub-bgl-2k-structured",
                        "train-calibration-holdout",
                        "BGL/test.csv",
                    )
                ],
            ),
            source("numenta-nab-real", nab_files),
            source(
                "aiops-challenge-2025",
                [
                    file_entry("aiops-2025-input", "input", "input.json"),
                    file_entry(
                        "aiops-2025-groundtruth", "scoring-oracle", "truth.jsonl"
                    ),
                ],
            ),
        ],
        "gates": {
            "log_holdout_f1_min": 0,
            "log_holdout_balanced_accuracy_min": 0,
            "timeseries_event_recall_min": 0,
            "timeseries_alert_precision_min": 0,
            "timeseries_event_f1_min": 0,
            "timeseries_false_alerts_per_1000_max": 1000,
            "insufficient_evidence_abstention_rate_min": 1,
            "unsafe_write_actions_max": 0,
            "oracle_leaks_max": 0,
        },
    }


def test_complete_external_pipeline_runs_offline_after_verified_fetch(tmp_path: Path) -> None:
    payloads, series_paths = _synthetic_external_payloads()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_synthetic_manifest(payloads, series_paths)), encoding="utf-8"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        file_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=payloads[file_id], request=request)

    cache_dir = tmp_path / "cache"
    report = run_external_validation(
        manifest_path,
        cache_dir,
        transport=httpx.MockTransport(handler),
    )
    assert report.verdict == "pass"
    assert report.sources_verified == 3
    assert report.source_files_verified == 8
    assert report.aggregate["unsafe_write_actions"] == 0

    evidence_path = tmp_path / "evidence.json"
    atomic_write_report(evidence_path, report)
    loaded = load_external_report(evidence_path)
    assert loaded is not None
    assert loaded.manifest_fingerprint == report.manifest_fingerprint

    offline = run_external_validation(manifest_path, cache_dir, offline=True)
    assert all(receipt.cache_hit for receipt in offline.provenance)
