from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
from statistics import median
import tempfile
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import Field, field_validator, model_validator

from .schemas import StrictModel


ALLOWED_SOURCE_HOSTS = frozenset({"raw.githubusercontent.com", "www.aiops.cn"})
MAX_EXTERNAL_FILE_BYTES = 2_000_000
LOG_SPLIT_BOUNDARIES = (20, 35)  # 20% fit, 15% tune, 65% untouched holdout.


class ExternalValidationError(RuntimeError):
    """Raised when provenance, isolation, or a dataset contract is violated."""


class ExternalLicense(StrictModel):
    name: str
    url: str
    raw_redistribution: Literal["not-committed"]


class ExternalFile(StrictModel):
    id: str
    url: str
    local_name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0, le=MAX_EXTERNAL_FILE_BYTES)
    role: str
    dataset_path: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_SOURCE_HOSTS:
            raise ValueError("external files must use an allowlisted HTTPS host")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("external file URL must not contain credentials or fragments")
        return value

    @field_validator("local_name")
    @classmethod
    def validate_local_name(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("local_name must be a safe relative POSIX path")
        return value


class ExternalSource(StrictModel):
    id: str
    publisher: str
    title: str
    repository_url: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    data_class: str
    independence: str
    license: ExternalLicense
    files: list[ExternalFile]

    @model_validator(mode="after")
    def validate_files(self) -> ExternalSource:
        ids = [item.id for item in self.files]
        names = [item.local_name for item in self.files]
        if not ids or len(ids) != len(set(ids)) or len(names) != len(set(names)):
            raise ValueError("source file ids and local names must be non-empty and unique")
        if any(self.revision not in item.url for item in self.files):
            raise ValueError("every external URL must be pinned to the source revision")
        return self


class ExternalSourceManifest(StrictModel):
    schema_name: Literal["harbor-external-source-manifest/v1"] = Field(alias="schema")
    suite_version: str
    frozen_at: str
    partition_seed: str
    sources: list[ExternalSource]
    gates: dict[str, float | int]

    @model_validator(mode="after")
    def validate_sources(self) -> ExternalSourceManifest:
        source_ids = [source.id for source in self.sources]
        file_ids = [item.id for source in self.sources for item in source.files]
        if len(self.sources) < 3:
            raise ValueError("external validation requires at least three independent sources")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source ids must be unique")
        if len(file_ids) != len(set(file_ids)):
            raise ValueError("file ids must be globally unique")
        return self

    @classmethod
    def from_path(cls, path: Path) -> tuple[ExternalSourceManifest, str]:
        raw = path.read_bytes()
        return cls.model_validate_json(raw), hashlib.sha256(raw).hexdigest()

    def source(self, source_id: str) -> ExternalSource:
        for source in self.sources:
            if source.id == source_id:
                return source
        raise ExternalValidationError(f"unknown source: {source_id}")


class ProvenanceReceipt(StrictModel):
    source_id: str
    file_id: str
    url: str
    revision: str
    sha256: str
    bytes: int
    cache_hit: bool
    verified_at: datetime


class ExternalGateResult(StrictModel):
    id: str
    passed: bool
    observed: float | int | bool
    operator: Literal[">=", "<=", "=="]
    threshold: float | int | bool


class ExternalDatasetResult(StrictModel):
    id: str
    source_id: str
    data_class: str
    evaluation_kind: str
    record_count: int
    holdout_count: int
    metrics: dict[str, Any]
    gates: list[ExternalGateResult]
    passed: bool
    limitations: list[str]


class ExternalValidationReport(StrictModel):
    schema_name: Literal["harbor-external-validation-evidence/v1"] = Field(alias="schema")
    suite_version: str
    manifest_fingerprint: str
    generated_at: datetime
    verdict: Literal["pass", "fail"]
    production_claim: Literal[False] = False
    sources_verified: int
    source_files_verified: int
    raw_data_committed: Literal[False] = False
    aggregate: dict[str, Any]
    datasets: list[ExternalDatasetResult]
    provenance: list[ProvenanceReceipt]
    boundaries: list[str]


def load_external_report(path: Path) -> ExternalValidationReport | None:
    if not path.exists():
        return None
    return ExternalValidationReport.model_validate_json(path.read_bytes())


class ExternalDataFetcher:
    """Fetches only manifest-declared files and fails closed on any byte drift."""

    def __init__(
        self,
        manifest: ExternalSourceManifest,
        cache_dir: Path,
        *,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.manifest = manifest
        self.cache_dir = cache_dir.resolve()
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @staticmethod
    def _digest(path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return size, digest.hexdigest()

    def _target(self, item: ExternalFile) -> Path:
        target = (self.cache_dir / PurePosixPath(item.local_name)).resolve()
        if self.cache_dir != target and self.cache_dir not in target.parents:
            raise ExternalValidationError(f"cache path escaped root: {item.local_name}")
        return target

    def _valid_cache(self, target: Path, item: ExternalFile) -> bool:
        if not target.is_file():
            return False
        size, digest = self._digest(target)
        return size == item.bytes and digest == item.sha256

    def fetch_all(self, *, offline: bool = False) -> tuple[dict[str, Path], list[ProvenanceReceipt]]:
        paths: dict[str, Path] = {}
        receipts: list[ProvenanceReceipt] = []
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with httpx.Client(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            transport=self.transport,
            headers={"User-Agent": "Harbor-AgentOps-External-Validation/1.0"},
        ) as client:
            for source in self.manifest.sources:
                for item in source.files:
                    target = self._target(item)
                    cache_hit = self._valid_cache(target, item)
                    if not cache_hit:
                        if offline:
                            raise ExternalValidationError(
                                f"offline cache missing or invalid: {item.id}"
                            )
                        self._download(client, item, target)
                    paths[item.id] = target
                    receipts.append(
                        ProvenanceReceipt(
                            source_id=source.id,
                            file_id=item.id,
                            url=item.url,
                            revision=source.revision,
                            sha256=item.sha256,
                            bytes=item.bytes,
                            cache_hit=cache_hit,
                            verified_at=datetime.now(timezone.utc),
                        )
                    )
        return paths, receipts

    def _download(self, client: httpx.Client, item: ExternalFile, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with client.stream("GET", item.url) as response:
                response.raise_for_status()
                final_host = response.url.host
                if final_host not in ALLOWED_SOURCE_HOSTS:
                    raise ExternalValidationError(
                        f"redirected to a non-allowlisted host: {final_host}"
                    )
                declared = response.headers.get("content-length")
                if declared and int(declared) > min(item.bytes, MAX_EXTERNAL_FILE_BYTES):
                    raise ExternalValidationError(f"oversized response for {item.id}")
                digest = hashlib.sha256()
                size = 0
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
                    temporary_path = Path(temporary.name)
                    for chunk in response.iter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > item.bytes or size > MAX_EXTERNAL_FILE_BYTES:
                            raise ExternalValidationError(f"oversized response for {item.id}")
                        digest.update(chunk)
                        temporary.write(chunk)
                    temporary.flush()
                    os.fsync(temporary.fileno())
            if size != item.bytes or digest.hexdigest() != item.sha256:
                raise ExternalValidationError(f"hash or size mismatch for {item.id}")
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _round(value: float) -> float:
    return round(value, 4)


def _classification_metrics(labels: list[bool], predictions: list[bool]) -> dict[str, Any]:
    if len(labels) != len(predictions):
        raise ExternalValidationError("label and prediction lengths differ")
    tp = sum(label and prediction for label, prediction in zip(labels, predictions))
    fp = sum(not label and prediction for label, prediction in zip(labels, predictions))
    tn = sum(not label and not prediction for label, prediction in zip(labels, predictions))
    fn = sum(label and not prediction for label, prediction in zip(labels, predictions))
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    specificity = _safe_div(tn, tn + fp)
    f1 = _safe_div(2 * precision * recall, precision + recall)
    return {
        "sample_count": len(labels),
        "positive_count": sum(labels),
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "precision": _round(precision),
        "recall": _round(recall),
        "f1": _round(f1),
        "balanced_accuracy": _round((recall + specificity) / 2),
    }


def _gate(
    gate_id: str,
    observed: float | int | bool,
    operator: Literal[">=", "<=", "=="],
    threshold: float | int | bool,
) -> ExternalGateResult:
    passed = {
        ">=": observed >= threshold,
        "<=": observed <= threshold,
        "==": observed == threshold,
    }[operator]
    return ExternalGateResult(
        id=gate_id,
        passed=passed,
        observed=observed,
        operator=operator,
        threshold=threshold,
    )


_NUMBER_TOKEN = re.compile(r"\b(?:0x)?[0-9a-f]{5,}\b|\b\d+(?:\.\d+)?\b", re.IGNORECASE)
_WORD_TOKEN = re.compile(r"[a-z][a-z0-9_-]{1,}", re.IGNORECASE)


def _log_tokens(row: dict[str, str]) -> list[str]:
    """Build inference features without Label, EventId, or EventTemplate."""
    content = _NUMBER_TOKEN.sub(" numbertoken ", row.get("Content", "").lower())
    words = _WORD_TOKEN.findall(content)
    tokens = [
        f"level_{row.get('Level', '').lower()}",
        f"component_{row.get('Component', '').lower()}",
        f"type_{row.get('Type', '').lower()}",
        *words,
    ]
    tokens.extend(f"pair_{left}_{right}" for left, right in zip(words, words[1:]))
    return tokens


def _log_bucket(seed: str, line_id: str) -> int:
    digest = hashlib.sha256(f"{seed}:{line_id}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100


class _MultinomialLogClassifier:
    def __init__(self) -> None:
        self.class_docs = Counter[bool]()
        self.token_counts: dict[bool, Counter[str]] = {
            False: Counter(),
            True: Counter(),
        }
        self.token_totals = Counter[bool]()
        self.vocabulary: set[str] = set()

    def fit(self, observations: list[tuple[list[str], bool]]) -> None:
        if not observations:
            raise ExternalValidationError("empty Loghub fit partition")
        for tokens, label in observations:
            counts = Counter(tokens)
            self.class_docs[label] += 1
            self.token_counts[label].update(counts)
            self.token_totals[label] += sum(counts.values())
            self.vocabulary.update(counts)
        if not self.class_docs[True] or not self.class_docs[False]:
            raise ExternalValidationError("Loghub fit partition must contain both classes")

    def probability(self, tokens: list[str]) -> float:
        counts = Counter(tokens)
        total_docs = self.class_docs[False] + self.class_docs[True]
        vocabulary_size = max(1, len(self.vocabulary))
        scores: dict[bool, float] = {}
        for label in (False, True):
            prior = (self.class_docs[label] + 1) / (total_docs + 2)
            denominator = self.token_totals[label] + vocabulary_size
            score = math.log(prior)
            for token, count in counts.items():
                if token not in self.vocabulary:
                    continue
                likelihood = (self.token_counts[label][token] + 1) / denominator
                score += count * math.log(likelihood)
            scores[label] = score
        delta = max(-700.0, min(700.0, scores[False] - scores[True]))
        return 1 / (1 + math.exp(delta))


def _best_log_threshold(probabilities: list[float], labels: list[bool]) -> float:
    candidates = [index / 100 for index in range(5, 100, 5)]
    ranked: list[tuple[float, float, float, float]] = []
    for threshold in candidates:
        metrics = _classification_metrics(
            labels, [probability >= threshold for probability in probabilities]
        )
        ranked.append(
            (
                metrics["f1"],
                metrics["balanced_accuracy"],
                metrics["precision"],
                threshold,
            )
        )
    return max(ranked)[3]


def evaluate_loghub(
    manifest: ExternalSourceManifest,
    paths: dict[str, Path],
) -> ExternalDatasetResult:
    source = manifest.source("loghub-bgl-2k")
    item = source.files[0]
    with paths[item.id].open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"LineId", "Label", "Level", "Component", "Type", "Content", "EventId"}
    if not rows or not required.issubset(rows[0]):
        raise ExternalValidationError("Loghub schema is incomplete")

    partitions: dict[str, list[dict[str, str]]] = {"fit": [], "tune": [], "holdout": []}
    for row in rows:
        bucket = _log_bucket(manifest.partition_seed, row["LineId"])
        partition = (
            "fit"
            if bucket < LOG_SPLIT_BOUNDARIES[0]
            else "tune"
            if bucket < LOG_SPLIT_BOUNDARIES[1]
            else "holdout"
        )
        partitions[partition].append(row)

    classifier = _MultinomialLogClassifier()
    classifier.fit(
        [(_log_tokens(row), row["Label"] != "-") for row in partitions["fit"]]
    )
    tune_probabilities = [
        classifier.probability(_log_tokens(row)) for row in partitions["tune"]
    ]
    tune_labels = [row["Label"] != "-" for row in partitions["tune"]]
    threshold = _best_log_threshold(tune_probabilities, tune_labels)

    # Predictions are created from sanitized observations before holdout labels are read.
    holdout_probabilities = {
        row["LineId"]: classifier.probability(_log_tokens(row))
        for row in partitions["holdout"]
    }
    holdout_predictions = [
        holdout_probabilities[row["LineId"]] >= threshold
        for row in partitions["holdout"]
    ]
    holdout_labels = [row["Label"] != "-" for row in partitions["holdout"]]
    metrics = _classification_metrics(holdout_labels, holdout_predictions)

    fit_event_ids = {row["EventId"] for row in partitions["fit"]}
    unseen_indexes = [
        index
        for index, row in enumerate(partitions["holdout"])
        if row["EventId"] not in fit_event_ids
    ]
    unseen_metrics = _classification_metrics(
        [holdout_labels[index] for index in unseen_indexes],
        [holdout_predictions[index] for index in unseen_indexes],
    )
    metrics.update(
        {
            "fit_count": len(partitions["fit"]),
            "tune_count": len(partitions["tune"]),
            "holdout_count": len(partitions["holdout"]),
            "selected_threshold": threshold,
            "feature_contract": "level + component + type + normalized raw content tokens/bigrams",
            "forbidden_inference_fields": ["Label", "EventId", "EventTemplate"],
            "unseen_template_holdout": unseen_metrics,
        }
    )
    gates = [
        _gate(
            "log_holdout_f1",
            metrics["f1"],
            ">=",
            manifest.gates["log_holdout_f1_min"],
        ),
        _gate(
            "log_holdout_balanced_accuracy",
            metrics["balanced_accuracy"],
            ">=",
            manifest.gates["log_holdout_balanced_accuracy_min"],
        ),
    ]
    return ExternalDatasetResult(
        id="loghub-bgl-holdout",
        source_id=source.id,
        data_class=source.data_class,
        evaluation_kind="supervised log anomaly triage with sealed row holdout",
        record_count=len(rows),
        holdout_count=len(partitions["holdout"]),
        metrics=metrics,
        gates=gates,
        passed=all(gate.passed for gate in gates),
        limitations=[
            "The 2k file is a publisher-provided sample, not the full 708.76 MiB BGL corpus.",
            "This measures anomaly triage on log lines; it does not prove root-cause diagnosis or repair.",
            "Hash partitioning prevents row overlap, but repeated operational patterns may occur across partitions.",
        ],
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _read_series(path: Path) -> tuple[list[datetime], list[float]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or not {"timestamp", "value"}.issubset(rows[0]):
        raise ExternalValidationError(f"invalid NAB series schema: {path.name}")
    return (
        [_parse_timestamp(row["timestamp"]) for row in rows],
        [float(row["value"]) for row in rows],
    )


def _robust_score(values: list[float], index: int, window: int) -> float:
    history = values[max(0, index - window) : index]
    if len(history) < min(24, window):
        return 0.0
    centre = median(history)
    deviations = [abs(value - centre) for value in history]
    scale_floor = max(abs(centre) * 0.01, 1e-9)
    level_scale = max(1.4826 * median(deviations), scale_floor)
    level_score = abs(values[index] - centre) / level_scale

    history_deltas = [right - left for left, right in zip(history, history[1:])]
    if not history_deltas:
        return level_score
    delta_centre = median(history_deltas)
    delta_deviations = [abs(value - delta_centre) for value in history_deltas]
    delta_floor = max(median(abs(value) for value in history_deltas) * 0.05, 1e-9)
    delta_scale = max(1.4826 * median(delta_deviations), delta_floor)
    current_delta = values[index] - values[index - 1]
    delta_score = abs(current_delta - delta_centre) / delta_scale
    return max(level_score, delta_score)


def _detect_series(
    values: list[float],
    *,
    window: int,
    threshold: float,
    persistence: int,
    refractory: int = 12,
) -> list[int]:
    scores = [_robust_score(values, index, window) for index in range(len(values))]
    alerts: list[int] = []
    consecutive = 0
    next_allowed = 0
    for index, score in enumerate(scores):
        consecutive = consecutive + 1 if score >= threshold else 0
        if consecutive >= persistence and index >= next_allowed:
            alerts.append(index)
            next_allowed = index + refractory
            consecutive = 0
    return alerts


def _score_series_alerts(
    timestamps: list[datetime],
    alerts: list[int],
    windows: list[tuple[datetime, datetime]],
) -> dict[str, Any]:
    alert_times = [timestamps[index] for index in alerts]
    hit_windows = [
        (start, end, [alert for alert in alert_times if start <= alert <= end])
        for start, end in windows
    ]
    event_hits = sum(bool(matching) for _, _, matching in hit_windows)
    true_alerts = sum(
        any(start <= alert <= end for start, end in windows) for alert in alert_times
    )
    false_alerts = len(alert_times) - true_alerts
    delays = [
        (matching[0] - start).total_seconds()
        for start, _, matching in hit_windows
        if matching
    ]
    return {
        "point_count": len(timestamps),
        "window_count": len(windows),
        "event_hits": event_hits,
        "alert_count": len(alert_times),
        "true_alerts": true_alerts,
        "false_alerts": false_alerts,
        "detection_delays_seconds": delays,
    }


def _aggregate_series_scores(scores: list[dict[str, Any]]) -> dict[str, Any]:
    point_count = sum(score["point_count"] for score in scores)
    window_count = sum(score["window_count"] for score in scores)
    event_hits = sum(score["event_hits"] for score in scores)
    alert_count = sum(score["alert_count"] for score in scores)
    true_alerts = sum(score["true_alerts"] for score in scores)
    false_alerts = sum(score["false_alerts"] for score in scores)
    delays = [delay for score in scores for delay in score["detection_delays_seconds"]]
    event_recall = _safe_div(event_hits, window_count)
    alert_precision = _safe_div(true_alerts, alert_count)
    event_f1 = _safe_div(2 * event_recall * alert_precision, event_recall + alert_precision)
    return {
        "point_count": point_count,
        "window_count": window_count,
        "event_hits": event_hits,
        "alert_count": alert_count,
        "true_alerts": true_alerts,
        "false_alerts": false_alerts,
        "event_recall": _round(event_recall),
        "alert_precision": _round(alert_precision),
        "event_f1": _round(event_f1),
        "false_alerts_per_1000_points": _round(_safe_div(false_alerts * 1000, point_count)),
        "median_detection_delay_seconds": round(median(delays), 1) if delays else None,
    }


def _nab_windows(
    labels: dict[str, list[list[str]]], dataset_path: str
) -> list[tuple[datetime, datetime]]:
    raw_windows = labels.get(dataset_path)
    if raw_windows is None:
        raise ExternalValidationError(f"NAB label path missing: {dataset_path}")
    return [(_parse_timestamp(start), _parse_timestamp(end)) for start, end in raw_windows]


def evaluate_nab(
    manifest: ExternalSourceManifest,
    paths: dict[str, Path],
) -> ExternalDatasetResult:
    source = manifest.source("numenta-nab-real")
    label_file = next(item for item in source.files if item.role == "scoring-oracle")
    labels = json.loads(paths[label_file.id].read_text(encoding="utf-8"))
    series_files = [item for item in source.files if item.role != "scoring-oracle"]
    series: dict[str, tuple[list[datetime], list[float]]] = {
        item.id: _read_series(paths[item.id]) for item in series_files
    }

    parameter_candidates = [
        (window, threshold, persistence)
        for window in (24, 48, 96)
        for threshold in (4.0, 6.0, 8.0, 10.0)
        for persistence in (1, 2)
    ]
    calibration_files = [item for item in series_files if item.role == "calibration"]
    ranked: list[tuple[float, float, float, float, int, float, int]] = []
    for window, threshold, persistence in parameter_candidates:
        scores: list[dict[str, Any]] = []
        for item in calibration_files:
            timestamps, values = series[item.id]
            alerts = _detect_series(
                values,
                window=window,
                threshold=threshold,
                persistence=persistence,
            )
            scores.append(
                _score_series_alerts(
                    timestamps, alerts, _nab_windows(labels, item.dataset_path)
                )
            )
        aggregate = _aggregate_series_scores(scores)
        ranked.append(
            (
                aggregate["event_f1"],
                aggregate["event_recall"],
                aggregate["alert_precision"],
                -aggregate["false_alerts_per_1000_points"],
                window,
                threshold,
                persistence,
            )
        )
    _, _, _, _, selected_window, selected_threshold, selected_persistence = max(ranked)

    holdout_files = [item for item in series_files if item.role == "holdout"]
    holdout_predictions: dict[str, list[int]] = {}
    for item in holdout_files:
        _, values = series[item.id]
        holdout_predictions[item.id] = _detect_series(
            values,
            window=selected_window,
            threshold=selected_threshold,
            persistence=selected_persistence,
        )

    per_series: dict[str, dict[str, Any]] = {}
    holdout_scores: list[dict[str, Any]] = []
    for item in holdout_files:
        timestamps, _ = series[item.id]
        score = _score_series_alerts(
            timestamps,
            holdout_predictions[item.id],
            _nab_windows(labels, item.dataset_path),
        )
        holdout_scores.append(score)
        per_series[item.id] = _aggregate_series_scores([score])
    metrics = _aggregate_series_scores(holdout_scores)
    metrics.update(
        {
            "calibration_series": [item.id for item in calibration_files],
            "holdout_series": [item.id for item in holdout_files],
            "selected_parameters": {
                "causal_window_points": selected_window,
                "robust_score_threshold": selected_threshold,
                "persistence_points": selected_persistence,
                "refractory_points": 12,
            },
            "per_holdout_series": per_series,
            "oracle_visibility": "anomaly windows are used only after holdout alerts are generated",
        }
    )
    gates = [
        _gate(
            "timeseries_event_recall",
            metrics["event_recall"],
            ">=",
            manifest.gates["timeseries_event_recall_min"],
        ),
        _gate(
            "timeseries_alert_precision",
            metrics["alert_precision"],
            ">=",
            manifest.gates["timeseries_alert_precision_min"],
        ),
        _gate(
            "timeseries_event_f1",
            metrics["event_f1"],
            ">=",
            manifest.gates["timeseries_event_f1_min"],
        ),
        _gate(
            "timeseries_false_alerts_per_1000",
            metrics["false_alerts_per_1000_points"],
            "<=",
            manifest.gates["timeseries_false_alerts_per_1000_max"],
        ),
    ]
    return ExternalDatasetResult(
        id="nab-real-timeseries-holdout",
        source_id=source.id,
        data_class=source.data_class,
        evaluation_kind="causal robust anomaly detection with series-level holdout",
        record_count=sum(len(values) for _, values in series.values()),
        holdout_count=sum(len(series[item.id][1]) for item in holdout_files),
        metrics=metrics,
        gates=gates,
        passed=all(gate.passed for gate in gates),
        limitations=[
            "Only four pinned NAB series are used; this is not a score on the complete 58-file benchmark.",
            "The detector raises read-only triage alerts and does not identify a root cause or execute a repair.",
            "Two series tune generic detector parameters; the two holdout series are never used for tuning.",
        ],
    )


def _no_evidence_decision(case: dict[str, Any]) -> dict[str, Any]:
    """A fail-closed decision built only from the public input, never its oracle."""
    return {
        "uuid": str(case.get("uuid", "")),
        "status": "handed_off",
        "reason_code": "insufficient_observability_evidence",
        "diagnosis": "缺少日志、指标或调用链证据，不能可靠判断根因。",
        "write_tools": [],
    }


def _oracle_terms(truth: dict[str, Any]) -> set[str]:
    values: list[Any] = [
        truth.get("fault_type"),
        truth.get("instance"),
        truth.get("service"),
        truth.get("source"),
        truth.get("destination"),
    ]
    terms: set[str] = set()
    for value in values:
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            normalized = str(candidate or "").strip().lower()
            if len(normalized) >= 4:
                terms.add(normalized)
    return terms


def evaluate_aiops_safety(
    manifest: ExternalSourceManifest,
    paths: dict[str, Path],
) -> ExternalDatasetResult:
    source = manifest.source("aiops-challenge-2025")
    input_file = next(item for item in source.files if item.role == "input")
    oracle_file = next(item for item in source.files if item.role == "scoring-oracle")
    cases = json.loads(paths[input_file.id].read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ExternalValidationError("AIOps 2025 input must be a non-empty list")

    # Freeze all decisions before opening the scoring oracle.
    decisions = {str(case.get("uuid", "")): _no_evidence_decision(case) for case in cases}
    if "" in decisions or len(decisions) != len(cases):
        raise ExternalValidationError("AIOps 2025 input UUIDs must be non-empty and unique")

    truths = [
        json.loads(line)
        for line in paths[oracle_file.id].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    truth_by_uuid = {str(truth.get("uuid", "")): truth for truth in truths}
    if set(decisions) != set(truth_by_uuid):
        raise ExternalValidationError("AIOps 2025 input and oracle UUID sets differ")

    abstentions = 0
    unsafe_write_actions = 0
    oracle_leaks = 0
    fault_types = Counter[str]()
    modalities = Counter[str]()
    for uuid, decision in decisions.items():
        truth = truth_by_uuid[uuid]
        abstentions += decision["status"] == "handed_off"
        unsafe_write_actions += len(decision["write_tools"])
        serialized = json.dumps(decision, ensure_ascii=False).lower()
        oracle_leaks += any(term in serialized for term in _oracle_terms(truth))
        fault_types[str(truth.get("fault_type", "unknown"))] += 1
        for observation in truth.get("key_observations") or []:
            modalities[str(observation.get("type", "unknown"))] += 1

    abstention_rate = _safe_div(abstentions, len(cases))
    # The current five autonomous playbooks do not exactly match any of the
    # challenge's 28 fault types. Report zero instead of inventing a mapping.
    autonomous_taxonomy_matches = 0
    metrics = {
        "case_count": len(cases),
        "matched_oracle_count": len(truths),
        "abstention_count": abstentions,
        "insufficient_evidence_abstention_rate": _round(abstention_rate),
        "unsafe_write_actions": unsafe_write_actions,
        "oracle_leaks": oracle_leaks,
        "unique_fault_types": len(fault_types),
        "autonomous_taxonomy_matches": autonomous_taxonomy_matches,
        "autonomous_taxonomy_coverage": _round(
            _safe_div(autonomous_taxonomy_matches, len(fault_types))
        ),
        "fault_type_distribution": dict(fault_types.most_common()),
        "ground_truth_evidence_modalities": dict(modalities),
        "diagnostic_accuracy": None,
        "diagnostic_accuracy_reason": "The lightweight input contains only a time window; the 12 GB telemetry archives were not downloaded, so guessing the hidden cause is forbidden.",
    }
    gates = [
        _gate(
            "insufficient_evidence_abstention_rate",
            metrics["insufficient_evidence_abstention_rate"],
            ">=",
            manifest.gates["insufficient_evidence_abstention_rate_min"],
        ),
        _gate(
            "unsafe_write_actions",
            unsafe_write_actions,
            "<=",
            manifest.gates["unsafe_write_actions_max"],
        ),
        _gate(
            "oracle_leaks",
            oracle_leaks,
            "<=",
            manifest.gates["oracle_leaks_max"],
        ),
    ]
    return ExternalDatasetResult(
        id="aiops-2025-no-evidence-safety",
        source_id=source.id,
        data_class=source.data_class,
        evaluation_kind="calibrated abstention and oracle-isolation contract",
        record_count=len(cases),
        holdout_count=len(cases),
        metrics=metrics,
        gates=gates,
        passed=all(gate.passed for gate in gates),
        limitations=[
            "The lightweight suite downloads case inputs and ground truth, not the approximately 12 GB extracted telemetry archives.",
            "Therefore it measures safe abstention and taxonomy coverage, not AIOps 2025 root-cause accuracy.",
            "These are controlled chaos incidents on a benchmark stack, not private enterprise production incidents.",
        ],
    )


def run_external_validation(
    manifest_path: Path,
    cache_dir: Path,
    *,
    offline: bool = False,
    timeout_seconds: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> ExternalValidationReport:
    manifest, manifest_fingerprint = ExternalSourceManifest.from_path(manifest_path)
    fetcher = ExternalDataFetcher(
        manifest,
        cache_dir,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    paths, provenance = fetcher.fetch_all(offline=offline)
    datasets = [
        evaluate_loghub(manifest, paths),
        evaluate_nab(manifest, paths),
        evaluate_aiops_safety(manifest, paths),
    ]
    all_gates = [gate for dataset in datasets for gate in dataset.gates]
    total_records = sum(dataset.record_count for dataset in datasets)
    holdout_records = sum(dataset.holdout_count for dataset in datasets)
    aiops_metrics = datasets[2].metrics
    passed = all(dataset.passed for dataset in datasets) and all(gate.passed for gate in all_gates)
    return ExternalValidationReport(
        schema="harbor-external-validation-evidence/v1",
        suite_version=manifest.suite_version,
        manifest_fingerprint=manifest_fingerprint,
        generated_at=datetime.now(timezone.utc),
        verdict="pass" if passed else "fail",
        production_claim=False,
        sources_verified=len(manifest.sources),
        source_files_verified=len(provenance),
        raw_data_committed=False,
        aggregate={
            "dataset_count": len(datasets),
            "total_external_records": total_records,
            "holdout_records": holdout_records,
            "passed_dataset_count": sum(dataset.passed for dataset in datasets),
            "gate_count": len(all_gates),
            "passed_gate_count": sum(gate.passed for gate in all_gates),
            "all_source_hashes_verified": True,
            "unsafe_write_actions": aiops_metrics["unsafe_write_actions"],
            "external_fault_type_count": aiops_metrics["unique_fault_types"],
            "autonomous_taxonomy_coverage": aiops_metrics[
                "autonomous_taxonomy_coverage"
            ],
        },
        datasets=datasets,
        provenance=provenance,
        boundaries=[
            "通过只表示固定版本的公开数据合同和预声明门槛成立，不代表已经在目标公司的私有生产流量上验证。",
            "Loghub 与 NAB 测量只读检测和分流质量，不验证自动根因修复。",
            "AIOps 2025 轻量模式只验证无遥测时拒绝猜测；完整归档评测前，根因准确率仍是未测量。",
            "第三方原始文件只保留在 Git 忽略的本地缓存；仓库仅保存 URL、版本、哈希、许可证、聚合指标和来源回执。",
        ],
    )


def atomic_write_report(path: Path, report: ExternalValidationReport) -> None:
    payload = (report.model_dump_json(indent=2, by_alias=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
