from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tarfile
import time

import httpx
import pytest

from app.telemetry_validation import (
    IncidentWindow,
    TelemetryDataFetcher,
    TelemetryDayIndex,
    TelemetryPredictionsArtifact,
    TelemetryPredictor,
    TelemetryRemoteFile,
    TelemetrySourceManifest,
    TelemetryValidationError,
    _Candidate,
    _parse_windows,
    load_telemetry_report,
    telemetry_semantic_fingerprint,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_final_manifest_is_revision_pinned_and_has_three_way_isolation() -> None:
    manifest, fingerprint = TelemetrySourceManifest.from_path(
        PROJECT_ROOT / "data" / "external" / "telemetry-manifest-v4.json"
    )

    assert len(fingerprint) == 64
    assert manifest.ruleset_version == "deterministic-rca-v3"
    assert [archive.role for archive in manifest.archives] == [
        "calibration",
        "validation",
        "validation",
        "holdout",
    ]
    assert all(manifest.source.revision in archive.url for archive in manifest.archives)
    assert manifest.metadata.oracle.role == "post-freeze-scoring-only"


def test_safe_extractor_rejects_path_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        payload = b"escape"
        member = tarfile.TarInfo("../outside.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(TelemetryValidationError, match="unsafe archive path"):
        TelemetryDataFetcher._extract_safely(archive_path, tmp_path / "2025-01-01")

    assert not (tmp_path.parent / "outside.txt").exists()


def test_safe_extractor_rejects_symbolic_links(tmp_path: Path) -> None:
    archive_path = tmp_path / "symlink.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        member = tarfile.TarInfo("2025-01-01/log-parquet/escape.parquet")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside.txt"
        archive.addfile(member)

    with pytest.raises(TelemetryValidationError, match="links and devices"):
        TelemetryDataFetcher._extract_safely(archive_path, tmp_path / "2025-01-01")


def test_downloader_rejects_redirect_outside_allowlist(tmp_path: Path) -> None:
    manifest, _ = TelemetrySourceManifest.from_path(
        PROJECT_ROOT / "data" / "external" / "telemetry-manifest-v4.json"
    )
    payload = b"pinned"
    item = TelemetryRemoteFile(
        url="https://www.aiops.cn/pinned.bin",
        local_name="pinned.bin",
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
        role="predictor-input",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.aiops.cn":
            return httpx.Response(302, headers={"Location": "https://example.com/pinned.bin"})
        return httpx.Response(200, content=payload)

    fetcher = TelemetryDataFetcher(
        manifest,
        tmp_path,
        transport=httpx.MockTransport(handler),
    )
    with httpx.Client(
        transport=fetcher.transport,
        follow_redirects=True,
    ) as client, pytest.raises(TelemetryValidationError, match="non-allowlisted host"):
        fetcher._download(client, item, tmp_path / "pinned.bin", None)

    assert not (tmp_path / "pinned.bin").exists()


def test_case_windows_reject_duplicate_identifiers(tmp_path: Path) -> None:
    manifest, _ = TelemetrySourceManifest.from_path(
        PROJECT_ROOT / "data" / "external" / "telemetry-manifest-v4.json"
    )
    rows = [
        {"uuid": "duplicate", "Anomaly Description": "2025-06-09T01:00:00Z to 2025-06-09T01:05:00Z"},
        {"uuid": "validation-1", "Anomaly Description": "2025-06-17T01:00:00Z to 2025-06-17T01:05:00Z"},
        {"uuid": "validation-2", "Anomaly Description": "2025-06-18T01:00:00Z to 2025-06-18T01:05:00Z"},
        {"uuid": "duplicate", "Anomaly Description": "2025-06-19T01:00:00Z to 2025-06-19T01:05:00Z"},
    ]
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(rows), encoding="utf-8")

    with pytest.raises(TelemetryValidationError, match="uuids must be unique"):
        _parse_windows(input_path, manifest)


def _write_parquet(connection: object, path: Path, query: str) -> None:
    escaped = str(path).replace("'", "''")
    connection.execute(f"COPY ({query}) TO '{escaped}' (FORMAT PARQUET)")


def test_node_entity_falls_through_null_pod_column(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    day = tmp_path / "2025-01-01"
    for folder in ("log-parquet", "metric-parquet", "trace-parquet"):
        (day / folder).mkdir(parents=True)
    connection = duckdb.connect()
    _write_parquet(
        connection,
        day / "log-parquet" / "logs.parquet",
        """
        SELECT 'hipstershop' AS k8_namespace,
               strftime(timestamp '2025-01-01 00:00:00', '%Y-%m-%dT%H:%M:%SZ') AS "@timestamp",
               'filebeat' AS agent_name, 'frontend-0' AS k8_pod,
               'normal request' AS message, 'aiops-k8s-01' AS k8_node_name
        """,
    )
    _write_parquet(
        connection,
        day / "trace-parquet" / "traces.parquet",
        """
        SELECT 'hipstershop.Frontend/Recv.' AS operationName,
               epoch_ms(timestamp '2025-01-01 00:00:00') AS startTimeMillis,
               1000::BIGINT AS duration,
               struct_pack(serviceName := 'frontend') AS process
        """,
    )
    _write_parquet(
        connection,
        day / "metric-parquet" / "node_cpu.parquet",
        """
        SELECT strftime(timestamp '2025-01-01 00:00:00' + i * interval 1 minute,
                        '%Y-%m-%dT%H:%M:%SZ') AS time,
               'null' AS pod, 'aiops-k8s-05' AS kubernetes_node,
               'node' AS object_type,
               CASE WHEN i >= 30 THEN 92.0 ELSE 20.0 END AS node_cpu_usage_rate
          FROM range(40) AS points(i)
        """,
    )
    connection.close()

    index = TelemetryDayIndex(day)
    try:
        window = IncidentWindow.model_validate(
            {
                "uuid": "node-case",
                "description": "synthetic contract only",
                "start": "2025-01-01T00:30:00Z",
                "end": "2025-01-01T00:35:00Z",
                "role": "holdout",
                "archive_id": "synthetic",
            }
        )
        candidates = index.candidates(window)
    finally:
        index.close()

    assert any(
        item.fault == "node cpu stress" and item.entity == "aiops-k8s-05"
        for item in candidates
    )


def test_fusion_is_invariant_to_equal_score_candidate_order() -> None:
    window = IncidentWindow.model_validate(
        {
            "uuid": "stable-tie",
            "description": "determinism contract",
            "start": "2025-01-01T00:30:00Z",
            "end": "2025-01-01T00:35:00Z",
            "role": "holdout",
            "archive_id": "synthetic",
        }
    )
    candidates = [
        _Candidate(
            "network delay",
            "z-service",
            50,
            "trace",
            "z-service -> target latency",
            "same score",
            "trace.parquet",
            source="z-service",
            destination="target",
        ),
        _Candidate(
            "code error",
            "a-service",
            50,
            "metric",
            "error_ratio",
            "same score",
            "metric.parquet",
        ),
        _Candidate(
            "cpu stress",
            "m-service",
            50,
            "log",
            "stress marker",
            "same score",
            "log.parquet",
        ),
    ]

    first = TelemetryPredictor._fuse(window, candidates, time.perf_counter())
    second = TelemetryPredictor._fuse(
        window,
        list(reversed(candidates)),
        time.perf_counter(),
    )

    assert first.model_dump(exclude={"latency_ms"}) == second.model_dump(
        exclude={"latency_ms"}
    )
    assert first.fault_type_top3 == ["code error", "cpu stress", "network delay"]
    assert first.entity_top3 == ["a-service", "m-service", "z-service"]


def test_semantic_fingerprint_excludes_runtime_fields_but_detects_rank_changes() -> None:
    artifact = TelemetryPredictionsArtifact.model_validate_json(
        (PROJECT_ROOT / "data" / "external" / "telemetry-predictions-v4.json").read_bytes()
    )
    replay = artifact.model_copy(deep=True)
    replay.generated_at = replay.generated_at.replace(microsecond=0)
    for prediction in replay.predictions:
        prediction.latency_ms += 10_000

    frozen = telemetry_semantic_fingerprint(artifact)
    assert telemetry_semantic_fingerprint(replay) == frozen

    replay.predictions[0].fault_type_top3 = list(
        reversed(replay.predictions[0].fault_type_top3)
    )
    assert telemetry_semantic_fingerprint(replay) != frozen


def test_tracked_holdout_was_frozen_before_oracle_and_passes_declared_gates() -> None:
    evidence_path = PROJECT_ROOT / "data" / "external" / "telemetry-evidence-v4.json"
    prediction_path = PROJECT_ROOT / "data" / "external" / "telemetry-predictions-v4.json"
    report = load_telemetry_report(evidence_path)

    assert report is not None
    assert report.verdict == "pass"
    assert report.production_claim is False
    assert report.raw_data_committed is False
    assert report.coverage["all_rows"] == 54_426_202
    assert report.calibration["case_count"] == 16
    assert report.validation["case_count"] == 48
    assert report.holdout["case_count"] == 24
    assert report.holdout["fault_type_top3_accuracy"] == 0.7917
    assert report.holdout["exact_rca_top1_accuracy"] == 0.2917
    assert report.replay_audit is not None
    assert report.prediction_semantic_fingerprint == (
        "3cbde44211333a675ddc4acf635e71546b6700d6507174cef7e0220ffc693d86"
    )
    assert report.replay_audit.semantic_match is True
    assert report.replay_audit.holdout.fault_type_top3_accuracy == 0.6667
    assert report.replay_audit.holdout.exact_rca_top1_accuracy == 0.25
    assert report.replay_audit.oracle_status == "already-opened-no-retuning"
    assert report.replay_audit.passed_gates == report.replay_audit.gate_count == 10
    assert len(report.gates) == 10 and all(gate.passed for gate in report.gates)
    assert report.protocol["oracle_opened_after_freeze"] is True
    assert report.protocol["predictor_oracle_access"] == "none"
    assert report.protocol["unsafe_write_actions"] == 0
    assert hashlib.sha256(prediction_path.read_bytes()).hexdigest() == report.prediction_fingerprint
    serialized = prediction_path.read_text(encoding="utf-8").lower()
    assert '"expected_fault' not in serialized
    assert '"key_observations"' not in serialized
    assert '"fault_description"' not in serialized
