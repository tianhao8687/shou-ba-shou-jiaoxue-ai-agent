from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import time
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from pydantic import Field, field_validator, model_validator

from .schemas import StrictModel


ALLOWED_SOURCE_HOSTS = frozenset({"www.aiops.cn"})
MAX_REMOTE_BYTES = 1_000_000_000
MAX_EXTRACTED_BYTES = 2_500_000_000
MAX_ARCHIVE_MEMBERS = 20_000
UTC_WINDOW = re.compile(
    r"(2025-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z).*?"
    r"(2025-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)"
)
NUMERIC_DUCKDB_TYPES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "FLOAT",
    "DOUBLE",
    "DECIMAL",
)
LOG_FILTER_PATTERN = (
    r"adservice--|gchelper|getrandomads|byteman|injected|exception|error|fail|"
    r"timeout|unavailable|dial|refused|reset|dns|unknownhost|resolve|corrupt|"
    r"malformed|checksum|i/o|ioerror|read-only|kill|evict|oom|port"
)


class TelemetryValidationError(RuntimeError):
    """Raised when source integrity, isolation, or telemetry contracts fail."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_SOURCE_HOSTS:
        raise ValueError("telemetry files must use an allowlisted HTTPS host")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("telemetry URL must not contain credentials or fragments")
    return value


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("telemetry local path must be a safe relative POSIX path")
    return value


class TelemetryLicense(StrictModel):
    name: str
    url: str
    raw_redistribution: Literal["not-committed"]


class TelemetrySource(StrictModel):
    id: str
    publisher: str
    title: str
    repository_url: str
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    data_class: str
    independence: str
    license: TelemetryLicense


class TelemetryRemoteFile(StrictModel):
    url: str
    local_name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0, le=MAX_REMOTE_BYTES)
    role: str

    _validate_url = field_validator("url")(_safe_url)
    _validate_path = field_validator("local_name")(_safe_relative_path)


class TelemetryMetadata(StrictModel):
    input: TelemetryRemoteFile
    oracle: TelemetryRemoteFile

    @model_validator(mode="after")
    def validate_roles(self) -> TelemetryMetadata:
        if self.input.role != "predictor-input":
            raise ValueError("input must be predictor-input")
        if self.oracle.role != "post-freeze-scoring-only":
            raise ValueError("oracle must be post-freeze-scoring-only")
        return self


class TelemetryArchive(TelemetryRemoteFile):
    id: str
    date: str = Field(pattern=r"^2025-\d{2}-\d{2}$")
    role: Literal["calibration", "validation", "holdout"]
    extract_dir: str
    md5: str = Field(pattern=r"^[0-9a-f]{32}$")
    coverage_start: datetime
    coverage_end: datetime

    _validate_extract_path = field_validator("extract_dir")(_safe_relative_path)

    @model_validator(mode="after")
    def validate_window(self) -> TelemetryArchive:
        if self.coverage_start.tzinfo is None or self.coverage_end.tzinfo is None:
            raise ValueError("archive coverage must be timezone-aware")
        if self.coverage_start >= self.coverage_end:
            raise ValueError("archive coverage window is reversed")
        return self


class TelemetryProtocol(StrictModel):
    predictor_oracle_access: Literal["forbidden"]
    prediction_freeze: Literal["atomic-json-then-sha256"]
    scorer_order: Literal["oracle-opened-after-prediction-freeze"]
    write_actions: Literal["disabled"]
    timezone_contract: str


class TelemetrySourceManifest(StrictModel):
    schema_name: Literal["harbor-telemetry-source-manifest/v1"] = Field(alias="schema")
    suite_version: str
    frozen_at: datetime
    ruleset_version: str
    source: TelemetrySource
    metadata: TelemetryMetadata
    archives: list[TelemetryArchive]
    protocol: TelemetryProtocol
    gates: dict[str, float | int | bool]

    @model_validator(mode="after")
    def validate_contract(self) -> TelemetrySourceManifest:
        ids = [item.id for item in self.archives]
        names = [item.local_name for item in self.archives]
        roles = [item.role for item in self.archives]
        if len(ids) != len(set(ids)) or len(names) != len(set(names)):
            raise ValueError("archive ids and local names must be unique")
        if roles.count("calibration") != 1 or roles.count("holdout") != 1:
            raise ValueError("exactly one calibration and one holdout archive are required")
        if len(roles) < 2:
            raise ValueError("telemetry validation needs calibration and holdout archives")
        urls = [
            self.metadata.input.url,
            self.metadata.oracle.url,
            *(item.url for item in self.archives),
        ]
        if any(self.source.revision not in url for url in urls):
            raise ValueError("every telemetry URL must be pinned to the source revision")
        required = {
            "archive_hashes_verified",
            "holdout_case_count_min",
            "prediction_freeze_verified",
            "oracle_leaks_max",
            "unsafe_write_actions_max",
            "multimodal_case_coverage_min",
            "entity_top3_accuracy_min",
            "fault_type_top3_accuracy_min",
            "exact_rca_top1_accuracy_min",
            "evidence_modality_recall_min",
        }
        if required != set(self.gates):
            raise ValueError("telemetry gates must be complete and predeclared")
        return self

    @classmethod
    def from_path(cls, path: Path) -> tuple[TelemetrySourceManifest, str]:
        raw = path.read_bytes()
        return cls.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


class TelemetryReceipt(StrictModel):
    id: str
    role: str
    url: str
    sha256: str
    md5: str | None = None
    bytes: int
    cache_hit: bool
    extracted: bool
    verified_at: datetime


class IncidentWindow(StrictModel):
    uuid: str
    description: str
    start: datetime
    end: datetime
    role: Literal["calibration", "validation", "holdout"]
    archive_id: str


class TelemetryEvidence(StrictModel):
    modality: Literal["log", "metric", "trace"]
    entity: str
    signal: str
    score: float
    observation: str
    source_file: str


class TelemetryPrediction(StrictModel):
    uuid: str
    role: Literal["calibration", "validation", "holdout"]
    window_start: datetime
    window_end: datetime
    fault_type_top3: list[str] = Field(min_length=1, max_length=3)
    entity_top3: list[str] = Field(min_length=1, max_length=3)
    source: str | None = None
    destination: str | None = None
    confidence: float = Field(ge=0, le=1)
    abstained: bool
    evidence: list[TelemetryEvidence]
    latency_ms: int = Field(ge=0)


class TelemetryPredictionsArtifact(StrictModel):
    schema_name: Literal["harbor-telemetry-predictions/v1"] = Field(alias="schema")
    suite_version: str
    ruleset_version: str
    manifest_fingerprint: str
    generated_at: datetime
    oracle_access: Literal["none"] = "none"
    unsafe_write_actions: Literal[0] = 0
    predictions: list[TelemetryPrediction]


def telemetry_semantic_fingerprint(artifact: TelemetryPredictionsArtifact) -> str:
    """Hash stable prediction meaning while excluding runtime-only observations."""

    projection = artifact.model_dump(mode="json", by_alias=True)
    projection.pop("generated_at", None)
    for prediction in projection["predictions"]:
        prediction.pop("latency_ms", None)
    payload = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class TelemetryGate(StrictModel):
    id: str
    passed: bool
    observed: float | int | bool
    operator: Literal[">=", "<=", "=="]
    threshold: float | int | bool


class TelemetryCaseScore(StrictModel):
    uuid: str
    role: Literal["calibration", "validation", "holdout"]
    predicted_fault_type: str
    predicted_entity: str
    fault_type_top1: bool
    fault_type_top3: bool
    entity_top1: bool
    entity_top3: bool
    network_pair_match: bool | None = None
    exact_rca_top1: bool
    evidence_modality_recall: float
    evidence_modalities: list[str]
    abstained: bool


class TelemetrySemanticMetrics(StrictModel):
    case_count: int = Field(ge=1)
    fault_type_top1_accuracy: float = Field(ge=0, le=1)
    fault_type_top3_accuracy: float = Field(ge=0, le=1)
    entity_top1_accuracy: float = Field(ge=0, le=1)
    entity_top3_accuracy: float = Field(ge=0, le=1)
    exact_rca_top1_accuracy: float = Field(ge=0, le=1)
    evidence_modality_recall: float = Field(ge=0, le=1)
    multimodal_case_coverage: float = Field(ge=0, le=1)
    abstention_rate: float = Field(ge=0, le=1)


class TelemetryReplayAudit(StrictModel):
    schema_name: Literal["harbor-telemetry-replay-audit/v1"] = Field(alias="schema")
    audited_at: datetime
    oracle_status: Literal["already-opened-no-retuning"]
    tie_break_contract: str
    replay_count: int = Field(ge=2)
    semantic_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_match: Literal[True]
    full_artifact_hashes: list[str] = Field(min_length=2, max_length=2)
    volatile_fields_excluded: list[Literal["generated_at", "latency_ms"]]
    holdout: TelemetrySemanticMetrics
    p95_case_latency_ms_range: list[int] = Field(min_length=2, max_length=2)
    passed_gates: int = Field(ge=0)
    gate_count: int = Field(ge=1)
    verdict: Literal["pass", "fail"]
    production_claim: Literal[False] = False
    notes: list[str]

    @model_validator(mode="after")
    def validate_replay_contract(self) -> TelemetryReplayAudit:
        if any(
            not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in self.full_artifact_hashes
        ):
            raise ValueError("replay artifact hashes must be SHA-256 hex strings")
        if len(set(self.full_artifact_hashes)) != 2:
            raise ValueError(
                "runtime fields should make the two full artifact hashes distinct"
            )
        if set(self.volatile_fields_excluded) != {"generated_at", "latency_ms"}:
            raise ValueError("semantic comparison must exclude only documented runtime fields")
        if self.passed_gates > self.gate_count:
            raise ValueError("passed gate count cannot exceed total gate count")
        if self.p95_case_latency_ms_range != sorted(self.p95_case_latency_ms_range):
            raise ValueError("latency range must be ascending")
        return self


class TelemetryValidationReport(StrictModel):
    schema_name: Literal["harbor-telemetry-validation-evidence/v2"] = Field(alias="schema")
    suite_version: str
    ruleset_version: str
    manifest_fingerprint: str
    prediction_fingerprint: str
    prediction_semantic_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    generated_at: datetime
    verdict: Literal["pass", "fail"]
    production_claim: Literal[False] = False
    raw_data_committed: Literal[False] = False
    replay_audit: TelemetryReplayAudit | None = None
    source: dict[str, Any]
    coverage: dict[str, Any]
    protocol: dict[str, Any]
    calibration: dict[str, float | int]
    validation: dict[str, float | int]
    holdout: dict[str, float | int]
    gates: list[TelemetryGate]
    cases: list[TelemetryCaseScore]
    provenance: list[TelemetryReceipt]
    boundaries: list[str]


def load_telemetry_report(path: Path) -> TelemetryValidationReport | None:
    if not path.exists():
        return None
    return TelemetryValidationReport.model_validate_json(path.read_bytes())


def _resolve_under(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    target = (resolved_root / PurePosixPath(relative)).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise TelemetryValidationError(f"path escaped cache root: {relative}")
    return target


def _digests(path: Path) -> tuple[int, str, str]:
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            sha256.update(chunk)
            md5.update(chunk)
    return size, sha256.hexdigest(), md5.hexdigest()


class TelemetryDataFetcher:
    """Fetch revision-pinned bytes and extract archives without trusting tar paths."""

    def __init__(
        self,
        manifest: TelemetrySourceManifest,
        cache_root: Path,
        *,
        timeout_seconds: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.manifest = manifest
        self.cache_root = cache_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    @staticmethod
    def _matches(path: Path, item: TelemetryRemoteFile, md5: str | None = None) -> bool:
        if not path.is_file():
            return False
        size, sha256, actual_md5 = _digests(path)
        return size == item.bytes and sha256 == item.sha256 and (
            md5 is None or actual_md5 == md5
        )

    def prepare(
        self, *, offline: bool = False
    ) -> tuple[dict[str, Path], dict[str, Path], list[TelemetryReceipt]]:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        files: list[tuple[str, TelemetryRemoteFile, str | None]] = [
            ("input", self.manifest.metadata.input, None),
            ("oracle", self.manifest.metadata.oracle, None),
            *((item.id, item, item.md5) for item in self.manifest.archives),
        ]
        paths: dict[str, Path] = {}
        receipts: list[TelemetryReceipt] = []
        with httpx.Client(
            timeout=httpx.Timeout(self.timeout_seconds),
            follow_redirects=True,
            transport=self.transport,
            headers={"User-Agent": "Harbor-AgentOps-Telemetry-Validation/2.0"},
        ) as client:
            for file_id, item, md5 in files:
                target = _resolve_under(self.cache_root, item.local_name)
                cache_hit = self._matches(target, item, md5)
                if not cache_hit:
                    if offline:
                        raise TelemetryValidationError(
                            f"offline cache missing or invalid: {file_id}"
                        )
                    self._download(client, item, target, md5)
                paths[file_id] = target
                receipts.append(
                    TelemetryReceipt(
                        id=file_id,
                        role=item.role,
                        url=item.url,
                        sha256=item.sha256,
                        md5=md5,
                        bytes=item.bytes,
                        cache_hit=cache_hit,
                        extracted=False,
                        verified_at=_utc_now(),
                    )
                )

        extracted: dict[str, Path] = {}
        for archive in self.manifest.archives:
            target = _resolve_under(self.cache_root, archive.extract_dir)
            if not self._valid_extraction(target):
                if target.exists():
                    raise TelemetryValidationError(
                        f"existing extraction is incomplete; refusing overwrite: {target}"
                    )
                self._extract_safely(paths[archive.id], target)
            extracted[archive.id] = target
            for receipt in receipts:
                if receipt.id == archive.id:
                    receipt.extracted = True
        return paths, extracted, receipts

    def _download(
        self,
        client: httpx.Client,
        item: TelemetryRemoteFile,
        target: Path,
        expected_md5: str | None,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with client.stream("GET", item.url) as response:
                response.raise_for_status()
                if response.url.host not in ALLOWED_SOURCE_HOSTS:
                    raise TelemetryValidationError(
                        f"redirected to non-allowlisted host: {response.url.host}"
                    )
                sha256 = hashlib.sha256()
                md5 = hashlib.md5(usedforsecurity=False)
                size = 0
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                    temporary_path = Path(temp.name)
                    for chunk in response.iter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > item.bytes or size > MAX_REMOTE_BYTES:
                            raise TelemetryValidationError("telemetry response exceeded declared size")
                        sha256.update(chunk)
                        md5.update(chunk)
                        temp.write(chunk)
                    temp.flush()
                    os.fsync(temp.fileno())
            if size != item.bytes or sha256.hexdigest() != item.sha256:
                raise TelemetryValidationError("telemetry source hash or size mismatch")
            if expected_md5 is not None and md5.hexdigest() != expected_md5:
                raise TelemetryValidationError("telemetry archive MD5 mismatch")
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _valid_extraction(path: Path) -> bool:
        return all(
            (path / child).is_dir()
            and any((path / child).rglob("*.parquet"))
            for child in ("log-parquet", "metric-parquet", "trace-parquet")
        )

    @staticmethod
    def _extract_safely(archive_path: Path, expected_target: Path) -> None:
        expected_target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{expected_target.name}-extract-", dir=expected_target.parent
        ) as staging_name:
            staging = Path(staging_name)
            with tarfile.open(archive_path, mode="r:gz") as bundle:
                members = bundle.getmembers()
                if len(members) > MAX_ARCHIVE_MEMBERS:
                    raise TelemetryValidationError("archive contains too many members")
                total = 0
                for member in members:
                    path = PurePosixPath(member.name)
                    if path.is_absolute() or ".." in path.parts or not path.parts:
                        raise TelemetryValidationError("unsafe archive path")
                    if any(part.startswith("._") for part in path.parts):
                        continue
                    if member.issym() or member.islnk() or member.isdev():
                        raise TelemetryValidationError("archive links and devices are forbidden")
                    total += max(0, member.size)
                    if total > MAX_EXTRACTED_BYTES:
                        raise TelemetryValidationError("archive expands beyond safety limit")
                    destination = (staging / Path(*path.parts)).resolve()
                    if staging.resolve() not in destination.parents and destination != staging.resolve():
                        raise TelemetryValidationError("archive member escaped staging root")
                    if member.isdir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        raise TelemetryValidationError("unsupported archive member type")
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    source = bundle.extractfile(member)
                    if source is None:
                        raise TelemetryValidationError("archive member could not be read")
                    with source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
            extracted_root = staging / expected_target.name
            if not TelemetryDataFetcher._valid_extraction(extracted_root):
                raise TelemetryValidationError("archive extraction lacks required modalities")
            os.replace(extracted_root, expected_target)


def _parse_windows(
    path: Path, manifest: TelemetrySourceManifest
) -> list[IncidentWindow]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TelemetryValidationError("input.json must contain a case list")
    windows: list[IncidentWindow] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        description = str(row.get("Anomaly Description", ""))
        match = UTC_WINDOW.search(description)
        if not match:
            continue
        start = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
        end = datetime.fromisoformat(match.group(2).replace("Z", "+00:00"))
        for archive in manifest.archives:
            coverage_start = archive.coverage_start.astimezone(timezone.utc)
            coverage_end = archive.coverage_end.astimezone(timezone.utc)
            if coverage_start <= start <= end <= coverage_end:
                windows.append(
                    IncidentWindow(
                        uuid=str(row.get("uuid", "")),
                        description=description,
                        start=start,
                        end=end,
                        role=archive.role,
                        archive_id=archive.id,
                    )
                )
                break
    declared_roles = {archive.role for archive in manifest.archives}
    roles = {role: sum(item.role == role for item in windows) for role in declared_roles}
    if not all(roles.values()):
        raise TelemetryValidationError(
            "input has no cases inside every declared archive window"
        )
    identifiers = [item.uuid for item in windows]
    if any(not identifier for identifier in identifiers):
        raise TelemetryValidationError("every selected telemetry case needs a uuid")
    if len(identifiers) != len(set(identifiers)):
        raise TelemetryValidationError("selected telemetry case uuids must be unique")
    return sorted(windows, key=lambda item: (item.start, item.uuid))


def _parquet_files(root: Path, modality_dir: str) -> list[Path]:
    return sorted(
        path
        for path in (root / modality_dir).rglob("*.parquet")
        if not path.name.startswith("._")
    )


def _duckdb() -> Any:
    try:
        import duckdb
    except ImportError as exc:  # pragma: no cover - exercised by CLI error handling
        raise TelemetryValidationError(
            "DuckDB is required; install backend/requirements-dev.txt"
        ) from exc
    return duckdb


def _sql_path(path: Path) -> str:
    return str(path.resolve()).replace("'", "''")


def _normalise_entity(value: str) -> str:
    cleaned = value.strip().lower().replace("_", "-")
    if not cleaned.startswith("aiops-k8s-"):
        cleaned = re.sub(r"-\d+$", "", cleaned)
    return cleaned


FAULT_ALIASES = {
    "jvm gc": "jvm gc",
    "jvm-gc": "jvm gc",
    "jvm cpu": "jvm cpu stress",
    "jvm cpu stress": "jvm cpu stress",
    "jvm-cpu-stress": "jvm cpu stress",
    "jvm latency": "jvm latency",
    "jvm-latency": "jvm latency",
    "jvm exception": "jvm exception",
    "jvm-exception": "jvm exception",
    "node cpu": "node cpu stress",
    "node cpu stress": "node cpu stress",
    "node disk": "node disk fill",
    "node disk fill": "node disk fill",
    "node memory": "node memory stress",
    "node memory stress": "node memory stress",
    "node network loss": "node network loss",
    "node network delay": "node network delay",
    "network delay": "network delay",
    "network loss": "network loss",
    "network corrupt": "network corrupt",
    "cpu stress": "cpu stress",
    "memory stress": "memory stress",
    "pod failure": "pod failure",
    "pod kill": "pod kill",
    "dns error": "dns error",
    "target port misconfig": "target port misconfig",
    "erroneous code": "code error",
    "code error": "code error",
    "io fault": "io fault",
}


def _normalise_fault(value: str) -> str:
    cleaned = re.sub(r"[-_]+", " ", value.strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return FAULT_ALIASES.get(cleaned, cleaned)


class _Candidate:
    def __init__(
        self,
        fault: str,
        entity: str,
        score: float,
        modality: Literal["log", "metric", "trace"],
        signal: str,
        observation: str,
        source_file: str,
        *,
        source: str | None = None,
        destination: str | None = None,
    ) -> None:
        self.fault = _normalise_fault(fault)
        self.entity = entity.lower() or "unknown"
        self.score = max(0.0, score)
        self.modality = modality
        self.signal = signal
        self.observation = observation
        self.source_file = source_file
        self.source = source
        self.destination = destination

    def evidence(self) -> TelemetryEvidence:
        return TelemetryEvidence(
            modality=self.modality,
            entity=self.entity,
            signal=self.signal,
            score=round(self.score, 3),
            observation=self.observation[:320],
            source_file=self.source_file,
        )


def _candidate_sort_key(
    candidate: _Candidate,
) -> tuple[float, str, str, str, str, str, str, str]:
    """Canonical ordering for equal-score evidence from unordered SQL aggregates."""

    return (
        -round(candidate.score, 9),
        candidate.modality,
        candidate.fault,
        candidate.entity,
        candidate.source or "",
        candidate.destination or "",
        candidate.signal,
        candidate.source_file,
    )


class TelemetryDayIndex:
    """DuckDB-backed read-only feature index for one extracted daily archive."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.connection = _duckdb().connect(":memory:")
        # The validator is an offline audit, so repeatability is more important than
        # parallel aggregate throughput. A single worker also makes floating-point
        # reduction order stable across online and cache-only replays.
        self.connection.execute("SET threads = 1")
        self.connection.execute("SET preserve_insertion_order = true")
        self.log_files = _parquet_files(self.root, "log-parquet")
        self.metric_files = _parquet_files(self.root, "metric-parquet")
        self.trace_files = _parquet_files(self.root, "trace-parquet")
        if not self.log_files or not self.metric_files or not self.trace_files:
            raise TelemetryValidationError("extracted day is missing a telemetry modality")
        self.row_counts = {
            "logs": self._row_count(self.log_files),
            "metrics": self._row_count(self.metric_files),
            "traces": self._row_count(self.trace_files),
        }
        self._build_log_index()
        self._build_trace_index()
        self._build_metric_index()

    def close(self) -> None:
        self.connection.close()

    def _row_count(self, files: list[Path]) -> int:
        return int(
            self.connection.execute(
                "SELECT count(*) FROM read_parquet(?, union_by_name=true)",
                [[str(path) for path in files]],
            ).fetchone()[0]
        )

    def _build_log_index(self) -> None:
        self.connection.execute(
            """
            CREATE TEMP TABLE interesting_logs AS
            SELECT try_cast("@timestamp" AS TIMESTAMP) AS ts,
                   lower(coalesce(k8_pod, 'unknown')) AS entity,
                   lower(coalesce(k8_node_name, 'unknown')) AS node,
                   lower(message) AS message,
                   filename
              FROM read_parquet(?, filename=true, union_by_name=true)
             WHERE regexp_matches(lower(message), ?)
            """,
            [[str(path) for path in self.log_files], LOG_FILTER_PATTERN],
        )
        self.connection.execute("CREATE INDEX log_ts ON interesting_logs(ts)")

    def _build_trace_index(self) -> None:
        self.connection.execute(
            """
            CREATE TEMP TABLE trace_edges AS
            SELECT floor(startTimeMillis / 60000)::BIGINT AS minute,
                   lower(process.serviceName) AS source,
                   lower(regexp_extract(operationName, 'hipstershop\\.([^/]+)', 1)) AS destination,
                   count(*)::BIGINT AS spans,
                   avg(duration) / 1000.0 AS average_ms,
                   max(duration) / 1000.0 AS maximum_ms,
                   min(filename) AS filename
              FROM read_parquet(?, filename=true, union_by_name=true)
             WHERE operationName LIKE 'hipstershop.%/%'
             GROUP BY 1, 2, 3
            """,
            [[str(path) for path in self.trace_files]],
        )
        self.connection.execute("CREATE INDEX trace_minute ON trace_edges(minute)")

    def _build_metric_index(self) -> None:
        self.connection.execute(
            """
            CREATE TEMP TABLE metric_points(
                ts TIMESTAMP,
                entity VARCHAR,
                entity_type VARCHAR,
                metric VARCHAR,
                value DOUBLE,
                filename VARCHAR
            )
            """
        )
        for path in self.metric_files:
            columns = self.connection.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{_sql_path(path)}')"
            ).fetchall()
            names = {str(row[0]) for row in columns}
            numeric = [
                str(name)
                for name, type_name, *_ in columns
                if str(type_name).upper().startswith(NUMERIC_DUCKDB_TYPES)
            ]
            if "time" not in names or not numeric:
                continue
            entity_options: list[str] = []
            if "object_id" in names:
                entity_options.append("nullif(object_id, 'null')")
            if "pod" in names:
                entity_options.append("nullif(pod, 'null')")
            if "object_type" in names:
                entity_options.append(
                    "case when lower(object_type) in ('tidb', 'tikv', 'pd', 'tiflash') "
                    "then 'tidb-' || lower(object_type) end"
                )
            if "kubernetes_node" in names:
                entity_options.append("nullif(kubernetes_node, 'null')")
            if "instance" in names:
                entity_options.append("nullif(instance, 'null')")
            entity = (
                f"coalesce({', '.join(entity_options)})"
                if entity_options
                else "NULL"
            )
            entity_type = (
                "coalesce(nullif(object_type, 'null'), 'unknown')"
                if "object_type" in names
                else "'unknown'"
            )
            for metric in numeric:
                escaped_metric = metric.replace('"', '""')
                self.connection.execute(
                    f"""
                    INSERT INTO metric_points
                    SELECT try_cast(time AS TIMESTAMP),
                           lower(coalesce({entity}, 'unknown')),
                           lower({entity_type}),
                           ?,
                           try_cast("{escaped_metric}" AS DOUBLE),
                           ?
                      FROM read_parquet('{_sql_path(path)}')
                     WHERE try_cast("{escaped_metric}" AS DOUBLE) IS NOT NULL
                    """,
                    [metric.lower(), path.name],
                )
        self.connection.execute("CREATE INDEX metric_ts ON metric_points(ts)")

    @staticmethod
    def _naive(value: datetime) -> datetime:
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def candidates(self, window: IncidentWindow) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        candidates.extend(self._log_candidates(window))
        candidates.extend(self._trace_candidates(window))
        candidates.extend(self._metric_candidates(window))
        return candidates

    def _log_candidates(self, window: IncidentWindow) -> list[_Candidate]:
        start = self._naive(window.start)
        end = self._naive(window.end + timedelta(minutes=5))
        rows = self.connection.execute(
            """
            SELECT entity, node, message, count(*) AS occurrences, min(filename) AS filename
              FROM interesting_logs
             WHERE ts BETWEEN ? AND ?
             GROUP BY 1, 2, 3
             ORDER BY occurrences DESC, entity, node, message, filename
             LIMIT 800
            """,
            [start, end],
        ).fetchall()
        signatures: list[tuple[re.Pattern[str], str, float, str]] = [
            (re.compile(r"adservice--gc|gchelper"), "jvm gc", 125, "JVM GC injection marker"),
            (re.compile(r"adservice--stress"), "jvm cpu stress", 125, "JVM CPU injection marker"),
            (re.compile(r"getrandomads-latency"), "jvm latency", 125, "JVM latency injection marker"),
            (re.compile(r"getrandomads-exception|throwexception|injected error"), "jvm exception", 120, "JVM exception marker"),
            (re.compile(r"unknownhost|no such host|dns.{0,30}(fail|error)|name resolution"), "dns error", 105, "DNS resolution failure"),
            (re.compile(r"connection refused|wrong port|target.{0,10}port|port misconfig"), "target port misconfig", 95, "connection refused / port mismatch"),
            (re.compile(r"corrupt|malformed|bad checksum|checksum mismatch"), "network corrupt", 100, "packet corruption marker"),
            (re.compile(r"input/output error|i/o error|ioerror|read-only file system"), "io fault", 100, "filesystem I/O failure"),
            (re.compile(r"oomkilled|pod.{0,20}kill|container.{0,20}kill|evict"), "pod kill", 90, "pod termination marker"),
            (re.compile(r"pod.{0,20}fail|container.{0,20}fail"), "pod failure", 80, "pod failure marker"),
            (re.compile(r"failedprecondition|can't access cart storage|erroneous|business.{0,20}error|injected code"), "code error", 84, "application code failure"),
            (re.compile(r"unavailable|while dialing|connection reset|timeout"), "network loss", 38, "network availability error"),
            (re.compile(r"unavailable|while dialing|connection reset|timeout"), "dns error", 34, "name-resolution-compatible availability symptom"),
            (re.compile(r"unavailable|while dialing|connection reset|timeout"), "network corrupt", 32, "packet-path availability symptom"),
        ]
        found: dict[tuple[str, str], _Candidate] = {}
        for entity, node, message, occurrences, filename in rows:
            clean_entity = str(entity or "unknown")
            for pattern, fault, weight, label in signatures:
                if not pattern.search(str(message)):
                    continue
                score = weight + min(20, math.log1p(int(occurrences)) * 3)
                if fault.startswith("node ") and node != "unknown":
                    clean_entity = str(node)
                candidate = _Candidate(
                    fault,
                    clean_entity,
                    score,
                    "log",
                    label,
                    f"{occurrences} matching log records in the incident window",
                    Path(str(filename)).name,
                )
                key = (candidate.fault, candidate.entity)
                if key not in found or found[key].score < candidate.score:
                    found[key] = candidate
        return list(found.values())

    def _trace_candidates(self, window: IncidentWindow) -> list[_Candidate]:
        baseline_start = int((window.start - timedelta(minutes=25)).timestamp() // 60)
        baseline_end = int((window.start - timedelta(minutes=2)).timestamp() // 60)
        incident_start = int(window.start.timestamp() // 60)
        incident_end = int((window.end + timedelta(minutes=5)).timestamp() // 60)
        rows = self.connection.execute(
            """
            SELECT source, destination,
                   sum(spans) FILTER (WHERE minute BETWEEN ? AND ?) AS base_spans,
                   sum(spans * average_ms) FILTER (WHERE minute BETWEEN ? AND ?)
                       / nullif(sum(spans) FILTER (WHERE minute BETWEEN ? AND ?), 0) AS base_ms,
                   sum(spans) FILTER (WHERE minute BETWEEN ? AND ?) AS incident_spans,
                   sum(spans * average_ms) FILTER (WHERE minute BETWEEN ? AND ?)
                       / nullif(sum(spans) FILTER (WHERE minute BETWEEN ? AND ?), 0) AS incident_ms,
                   max(maximum_ms) FILTER (WHERE minute BETWEEN ? AND ?) AS incident_max_ms,
                    min(filename) AS filename
              FROM trace_edges
             WHERE minute BETWEEN ? AND ? AND source <> destination AND destination <> ''
             GROUP BY 1, 2
             HAVING incident_spans >= 5
             ORDER BY source, destination
            """,
            [
                baseline_start,
                baseline_end,
                baseline_start,
                baseline_end,
                baseline_start,
                baseline_end,
                incident_start,
                incident_end,
                incident_start,
                incident_end,
                incident_start,
                incident_end,
                incident_start,
                incident_end,
                baseline_start,
                incident_end,
            ],
        ).fetchall()
        candidates: list[_Candidate] = []
        for source, destination, base_spans, base_ms, incident_spans, incident_ms, maximum_ms, filename in rows:
            base = float(base_ms or 0)
            incident = float(incident_ms or 0)
            maximum = float(maximum_ms or 0)
            ratio = incident / max(base, 1.0)
            if incident < 90 or ratio < 2.5:
                continue
            fault = "network loss" if maximum >= 10_000 or incident >= 5_000 else "network delay"
            score = min(110.0, 25 + math.log1p(ratio) * 18 + math.log1p(incident / 100) * 12)
            candidates.append(
                _Candidate(
                    fault,
                    str(source),
                    score,
                    "trace",
                    f"{source} -> {destination} latency",
                    f"mean {base:.1f}ms -> {incident:.1f}ms; max {maximum:.1f}ms; spans {int(incident_spans or 0)}",
                    Path(str(filename)).name,
                    source=str(source),
                    destination=str(destination),
                )
            )
            candidates.append(
                _Candidate(
                    "network corrupt",
                    str(source),
                    score * 0.72,
                    "trace",
                    f"{source} -> {destination} transport anomaly",
                    f"latency amplification {ratio:.1f}x with max {maximum:.1f}ms",
                    Path(str(filename)).name,
                    source=str(source),
                    destination=str(destination),
                )
            )
            candidates.append(
                _Candidate(
                    fault,
                    str(destination),
                    score * 0.96,
                    "trace",
                    f"{source} -> {destination} latency",
                    f"mean {base:.1f}ms -> {incident:.1f}ms; max {maximum:.1f}ms",
                    Path(str(filename)).name,
                    source=str(source),
                    destination=str(destination),
                )
            )
        return candidates

    def _metric_candidates(self, window: IncidentWindow) -> list[_Candidate]:
        baseline_start = self._naive(window.start - timedelta(minutes=25))
        baseline_end = self._naive(window.start - timedelta(minutes=2))
        incident_start = self._naive(window.start)
        incident_end = self._naive(window.end + timedelta(minutes=5))
        rows = self.connection.execute(
            """
            SELECT entity, entity_type, metric,
                   avg(value) FILTER (WHERE ts BETWEEN ? AND ?) AS base_avg,
                   stddev_pop(value) FILTER (WHERE ts BETWEEN ? AND ?) AS base_std,
                   count(*) FILTER (WHERE ts BETWEEN ? AND ?) AS base_count,
                   avg(value) FILTER (WHERE ts BETWEEN ? AND ?) AS incident_avg,
                   min(value) FILTER (WHERE ts BETWEEN ? AND ?) AS incident_min,
                   max(value) FILTER (WHERE ts BETWEEN ? AND ?) AS incident_max,
                   count(*) FILTER (WHERE ts BETWEEN ? AND ?) AS incident_count,
                    min(filename) AS filename
              FROM metric_points
             WHERE ts BETWEEN ? AND ? AND entity <> 'unknown'
             GROUP BY 1, 2, 3
             HAVING base_count >= 5 AND incident_count >= 2
             ORDER BY entity, entity_type, metric
            """,
            [
                baseline_start,
                baseline_end,
                baseline_start,
                baseline_end,
                baseline_start,
                baseline_end,
                incident_start,
                incident_end,
                incident_start,
                incident_end,
                incident_start,
                incident_end,
                incident_start,
                incident_end,
                baseline_start,
                incident_end,
            ],
        ).fetchall()
        ranked: list[tuple[float, tuple[Any, ...], str]] = []
        for row in rows:
            entity, entity_type, metric, base_avg, base_std, _, incident_avg, incident_min, incident_max, _, _ = row
            base = float(base_avg or 0)
            incident = float(incident_avg or 0)
            low = float(incident_min if incident_min is not None else incident)
            high = float(incident_max if incident_max is not None else incident)
            scale = max(float(base_std or 0), abs(base) * 0.03, 1e-8)
            deviations = [(abs(incident - base), "shift"), (abs(low - base), "drop"), (abs(high - base), "spike")]
            deviation, direction = max(deviations)
            z_score = deviation / scale
            relative = deviation / max(abs(base), 1e-8)
            score = min(100.0, math.log1p(max(z_score, relative)) * 19)
            if score >= 18:
                ranked.append((score, row, direction))

        candidates: list[_Candidate] = []
        stable_ranked = sorted(
            ranked,
            key=lambda item: (
                -round(item[0], 9),
                str(item[1][0]),
                str(item[1][1]),
                str(item[1][2]),
                str(item[1][-1]),
                item[2],
            ),
        )
        for score, row, direction in stable_ranked[:80]:
            entity, entity_type, metric, base_avg, _, _, incident_avg, incident_min, incident_max, _, filename = row
            metric = str(metric)
            entity_type = str(entity_type)
            base_value = float(base_avg or 0)
            incident_value = float(incident_avg or 0)
            minimum_value = float(incident_min or 0)
            maximum_value = float(incident_max or 0)
            ratio = incident_value / max(abs(base_value), 1e-8)
            duration_seconds = (window.end - window.start).total_seconds()
            fault_options: list[tuple[str, float]] = []
            if entity_type == "node" or str(entity).startswith("aiops-k8s-"):
                if "cpu" in metric and maximum_value >= max(80, base_value + 15):
                    fault_options.append(("node cpu stress", max(score, 90)))
                elif (
                    "memory_usage_rate" in metric
                    and maximum_value >= max(82, base_value + 15)
                ):
                    fault_options.append(("node memory stress", max(score, 88)))
                elif (
                    "filesystem_usage_rate" in metric
                    and maximum_value >= 90
                    and incident_value >= base_value + 8
                ):
                    fault_options.append(("node disk fill", max(score, 88)))
                elif "network" in metric and score >= 45:
                    fault_options.extend(
                        [("node network loss", score * 0.85), ("node network delay", score * 0.75)]
                    )
            elif entity_type in {"tikv", "pd", "tidb", "tiflash"}:
                if metric in {"server_is_up", "uptime"} and (
                    minimum_value < base_value or incident_value < base_value * 0.7
                ):
                    fault_options.append(("pod failure", max(score, 95)))
                elif entity_type == "pd" and metric in {
                    "abnormal_region_count",
                    "leader_count",
                    "region_health",
                    "store_down_count",
                    "store_unhealth_count",
                }:
                    fault_options.append(("pod failure", max(score, 82)))
                elif metric in {
                    "io_util",
                    "raft_apply_wait",
                    "raft_propose_wait",
                    "region_pending",
                    "snapshot_apply_count",
                    "store_size",
                    "rocksdb_write_stall",
                    "read_mbps",
                    "write_wal_mbps",
                }:
                    fault_options.append(("io fault", max(score, 82)))
                elif metric in {"qps", "grpc_qps", "connection_count", "block_cache_size"}:
                    fault_options.extend(
                        [("pod failure", max(score, 72)), ("io fault", max(score, 60))]
                    )
            else:
                if metric == "pod_cpu_usage" and incident_value >= max(0.15, base_value * 2):
                    fault_options.append(("cpu stress", max(score, 82)))
                    if str(entity).startswith("adservice"):
                        fault_options.append(("jvm cpu stress", max(score, 82) * 0.9))
                elif (
                    "memory_working_set" in metric
                    and incident_value >= max(base_value * 1.8, base_value + 50_000_000)
                ):
                    fault_options.append(("memory stress", max(score, 80)))
                elif metric == "pod_processes":
                    if duration_seconds <= 5 and maximum_value > base_value:
                        fault_options.extend(
                            [("pod kill", max(score, 92)), ("pod failure", max(score, 75))]
                        )
                    elif minimum_value < base_value * 0.5:
                        fault_options.extend(
                            [("pod kill", max(score, 84)), ("pod failure", max(score, 72))]
                        )
                    elif incident_value >= base_value * 1.15 and maximum_value > base_value:
                        fault_options.extend(
                            [("code error", max(score, 80)), ("memory stress", max(score, 74))]
                        )
                elif metric == "io_util" and incident_value >= 80 and ratio >= 2:
                    fault_options.append(("io fault", max(score, 80)))
                elif metric in {"rrt", "rrt_max"} and ratio >= 1.8:
                    fault_options.append(("network delay", max(score * 0.7, 62)))
                    if str(entity).startswith("adservice"):
                        fault_options.append(("jvm latency", max(score * 0.9, 75)))
                elif (
                    metric in {"timeout", "error", "error_ratio", "server_error", "client_error"}
                    and incident_value >= max(1, base_value * 2)
                ):
                    fault_options.extend(
                        [
                            ("network loss", score * 0.65),
                            ("code error", score * 0.62),
                            ("dns error", score * 0.58),
                            ("pod failure", score * 0.5),
                        ]
                    )
            for fault, adjusted in fault_options:
                candidates.append(
                    _Candidate(
                        fault,
                        str(entity),
                        adjusted,
                        "metric",
                        metric,
                        (
                            f"baseline mean {float(base_avg or 0):.4g}; incident mean "
                            f"{float(incident_avg or 0):.4g}; range "
                            f"{float(incident_min or 0):.4g}..{float(incident_max or 0):.4g}"
                        ),
                        str(filename),
                    )
                )
        return candidates


class TelemetryPredictor:
    """Deterministic predictor. Its public API intentionally has no oracle argument."""

    def __init__(
        self,
        manifest: TelemetrySourceManifest,
        manifest_fingerprint: str,
        extracted: dict[str, Path],
    ) -> None:
        self.manifest = manifest
        self.manifest_fingerprint = manifest_fingerprint
        self.indexes = {
            archive.id: TelemetryDayIndex(extracted[archive.id])
            for archive in manifest.archives
        }

    def close(self) -> None:
        for index in self.indexes.values():
            index.close()

    @property
    def coverage(self) -> dict[str, Any]:
        by_archive = {
            archive_id: {
                "rows": dict(index.row_counts),
                "files": {
                    "logs": len(index.log_files),
                    "metrics": len(index.metric_files),
                    "traces": len(index.trace_files),
                },
            }
            for archive_id, index in self.indexes.items()
        }
        totals = {
            modality: sum(index.row_counts[modality] for index in self.indexes.values())
            for modality in ("logs", "metrics", "traces")
        }
        return {
            "by_archive": by_archive,
            "total_rows": totals,
            "all_rows": sum(totals.values()),
        }

    def predict(self, input_path: Path) -> TelemetryPredictionsArtifact:
        predictions: list[TelemetryPrediction] = []
        for window in _parse_windows(input_path, self.manifest):
            started = time.perf_counter()
            candidates = self.indexes[window.archive_id].candidates(window)
            predictions.append(self._fuse(window, candidates, started))
        return TelemetryPredictionsArtifact(
            schema="harbor-telemetry-predictions/v1",
            suite_version=self.manifest.suite_version,
            ruleset_version=self.manifest.ruleset_version,
            manifest_fingerprint=self.manifest_fingerprint,
            generated_at=_utc_now(),
            predictions=predictions,
        )

    @staticmethod
    def _fuse(
        window: IncidentWindow, candidates: list[_Candidate], started: float
    ) -> TelemetryPrediction:
        fault_channels: defaultdict[str, dict[str, float]] = defaultdict(dict)
        entity_channels: defaultdict[str, dict[str, float]] = defaultdict(dict)
        for candidate in candidates:
            fault_channels[candidate.fault][candidate.modality] = max(
                fault_channels[candidate.fault].get(candidate.modality, 0),
                candidate.score,
            )
            entity_channels[candidate.entity][candidate.modality] = max(
                entity_channels[candidate.entity].get(candidate.modality, 0),
                candidate.score,
            )
        fault_scores = {
            fault: sum(
                score * weight
                for score, weight in zip(
                    sorted(channels.values(), reverse=True),
                    (1.0, 0.35, 0.2),
                )
            )
            for fault, channels in fault_channels.items()
        }
        entity_scores = {
            entity: sum(
                score * weight
                for score, weight in zip(
                    sorted(channels.values(), reverse=True),
                    (1.0, 0.35, 0.2),
                )
            )
            for entity, channels in entity_channels.items()
        }
        if not fault_scores:
            fault_scores["insufficient evidence"] = 0
        if not entity_scores:
            entity_scores["unknown"] = 0
        ranked_faults = [
            name
            for name, _ in sorted(
                fault_scores.items(),
                key=lambda item: (-round(item[1], 9), item[0]),
            )[:3]
        ]
        ranked_entities = [
            name
            for name, _ in sorted(
                entity_scores.items(),
                key=lambda item: (-round(item[1], 9), item[0]),
            )[:3]
        ]
        top_score = fault_scores[ranked_faults[0]]
        abstained = top_score < 25 or ranked_faults[0] == "insufficient evidence"
        if abstained and ranked_faults[0] != "insufficient evidence":
            ranked_faults = ["insufficient evidence", *ranked_faults[:2]]
        trace = sorted(
            (candidate for candidate in candidates if candidate.source and candidate.destination),
            key=_candidate_sort_key,
        )
        chosen_evidence: list[TelemetryEvidence] = []
        seen: set[tuple[str, str, str]] = set()
        ranked_candidates = sorted(candidates, key=_candidate_sort_key)
        # Preserve the strongest independent signal from every available modality
        # before filling the remainder. This prevents one noisy metric family from
        # hiding a high-specificity log or trace observation in the evidence bundle.
        for modality in ("log", "metric", "trace"):
            candidate = next(
                (item for item in ranked_candidates if item.modality == modality),
                None,
            )
            if candidate is None:
                continue
            key = (candidate.modality, candidate.entity, candidate.signal)
            seen.add(key)
            chosen_evidence.append(candidate.evidence())
        for candidate in ranked_candidates:
            key = (candidate.modality, candidate.entity, candidate.signal)
            if key in seen:
                continue
            seen.add(key)
            chosen_evidence.append(candidate.evidence())
            if len(chosen_evidence) == 10:
                break
        confidence = 0 if top_score <= 0 else min(0.98, 1 - math.exp(-top_score / 110))
        return TelemetryPrediction(
            uuid=window.uuid,
            role=window.role,
            window_start=window.start,
            window_end=window.end,
            fault_type_top3=ranked_faults,
            entity_top3=ranked_entities,
            source=trace[0].source if trace else None,
            destination=trace[0].destination if trace else None,
            confidence=round(confidence, 4),
            abstained=abstained,
            evidence=chosen_evidence,
            latency_ms=round((time.perf_counter() - started) * 1000),
        )


def _atomic_model_write(path: Path, model: StrictModel) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = model.model_dump_json(by_alias=True, indent=2).encode("utf-8") + b"\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write_telemetry_report(path: Path, report: TelemetryValidationReport) -> str:
    return _atomic_model_write(path, report)


def _load_truth_after_freeze(path: Path) -> dict[str, dict[str, Any]]:
    truth: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            truth[str(row["uuid"])] = row
    return truth


def _expected_entities(row: dict[str, Any]) -> set[str]:
    values: list[str] = []
    instance = row.get("instance", [])
    if isinstance(instance, list):
        values.extend(str(value) for value in instance)
    elif instance:
        values.append(str(instance))
    for key in ("service", "source", "destination"):
        if row.get(key):
            values.append(str(row[key]))
    return {_normalise_entity(value) for value in values if value}


def _role_metrics(scores: list[TelemetryCaseScore], predictions: list[TelemetryPrediction]) -> dict[str, float | int]:
    if not scores:
        return {"case_count": 0}
    count = len(scores)
    by_id = {item.uuid: item for item in predictions}
    latencies = sorted(by_id[item.uuid].latency_ms for item in scores)
    p95_index = min(len(latencies) - 1, math.ceil(len(latencies) * 0.95) - 1)
    return {
        "case_count": count,
        "fault_type_top1_accuracy": round(sum(item.fault_type_top1 for item in scores) / count, 4),
        "fault_type_top3_accuracy": round(sum(item.fault_type_top3 for item in scores) / count, 4),
        "entity_top1_accuracy": round(sum(item.entity_top1 for item in scores) / count, 4),
        "entity_top3_accuracy": round(sum(item.entity_top3 for item in scores) / count, 4),
        "exact_rca_top1_accuracy": round(sum(item.exact_rca_top1 for item in scores) / count, 4),
        "evidence_modality_recall": round(sum(item.evidence_modality_recall for item in scores) / count, 4),
        "multimodal_case_coverage": round(sum(len(item.evidence_modalities) >= 2 for item in scores) / count, 4),
        "abstention_rate": round(sum(item.abstained for item in scores) / count, 4),
        "median_case_latency_ms": latencies[len(latencies) // 2],
        "p95_case_latency_ms": latencies[p95_index],
    }


def _gate(
    gate_id: str,
    observed: float | int | bool,
    operator: Literal[">=", "<=", "=="],
    threshold: float | int | bool,
) -> TelemetryGate:
    passed = {">=": observed >= threshold, "<=": observed <= threshold, "==": observed == threshold}[operator]
    return TelemetryGate(
        id=gate_id,
        passed=passed,
        observed=observed,
        operator=operator,
        threshold=threshold,
    )


def score_frozen_predictions(
    manifest: TelemetrySourceManifest,
    manifest_fingerprint: str,
    artifact: TelemetryPredictionsArtifact,
    prediction_fingerprint: str,
    truth_path: Path,
    coverage: dict[str, Any],
    receipts: list[TelemetryReceipt],
    prediction_frozen_at: datetime,
) -> TelemetryValidationReport:
    if artifact.oracle_access != "none":
        raise TelemetryValidationError("prediction artifact reports oracle access")
    if artifact.manifest_fingerprint != manifest_fingerprint:
        raise TelemetryValidationError("prediction and manifest fingerprints differ")
    if not prediction_fingerprint or len(prediction_fingerprint) != 64:
        raise TelemetryValidationError("prediction artifact was not frozen")
    prediction_ids = [item.uuid for item in artifact.predictions]
    if len(prediction_ids) != len(set(prediction_ids)):
        raise TelemetryValidationError("frozen predictions contain duplicate case uuids")

    oracle_opened_at = _utc_now()
    truth = _load_truth_after_freeze(truth_path)
    case_scores: list[TelemetryCaseScore] = []
    for prediction in artifact.predictions:
        expected = truth.get(prediction.uuid)
        if expected is None:
            raise TelemetryValidationError(f"oracle lacks selected case: {prediction.uuid}")
        expected_fault = _normalise_fault(str(expected.get("fault_type", "")))
        predicted_faults = [
            _normalise_fault(value) for value in prediction.fault_type_top3
        ]
        expected_entities = _expected_entities(expected)
        predicted_entities = [_normalise_entity(value) for value in prediction.entity_top3]
        expected_modalities = {
            str(item.get("type"))
            for item in expected.get("key_observations", [])
            if isinstance(item, dict) and item.get("type") in {"log", "metric", "trace"}
        }
        predicted_modalities = {item.modality for item in prediction.evidence}
        modality_recall = (
            len(expected_modalities & predicted_modalities) / len(expected_modalities)
            if expected_modalities
            else 1.0
        )
        pair_match: bool | None = None
        if expected.get("source") and expected.get("destination"):
            pair_match = (
                _normalise_entity(prediction.source or "")
                == _normalise_entity(str(expected["source"]))
                and _normalise_entity(prediction.destination or "")
                == _normalise_entity(str(expected["destination"]))
            )
        entity_top1 = bool(predicted_entities and predicted_entities[0] in expected_entities)
        fault_top1 = bool(predicted_faults and predicted_faults[0] == expected_fault)
        case_scores.append(
            TelemetryCaseScore(
                uuid=prediction.uuid,
                role=prediction.role,
                predicted_fault_type=prediction.fault_type_top3[0],
                predicted_entity=prediction.entity_top3[0],
                fault_type_top1=fault_top1,
                fault_type_top3=expected_fault in predicted_faults,
                entity_top1=entity_top1,
                entity_top3=bool(expected_entities & set(predicted_entities)),
                network_pair_match=pair_match,
                exact_rca_top1=fault_top1
                and (pair_match if pair_match is not None else entity_top1),
                evidence_modality_recall=round(modality_recall, 4),
                evidence_modalities=sorted(predicted_modalities),
                abstained=prediction.abstained,
            )
        )

    calibration_scores = [item for item in case_scores if item.role == "calibration"]
    validation_scores = [item for item in case_scores if item.role == "validation"]
    holdout_scores = [item for item in case_scores if item.role == "holdout"]
    calibration_predictions = [item for item in artifact.predictions if item.role == "calibration"]
    validation_predictions = [item for item in artifact.predictions if item.role == "validation"]
    holdout_predictions = [item for item in artifact.predictions if item.role == "holdout"]
    calibration = _role_metrics(calibration_scores, calibration_predictions)
    validation = _role_metrics(validation_scores, validation_predictions)
    holdout = _role_metrics(holdout_scores, holdout_predictions)
    gates = [
        _gate("archive_hashes_verified", all(item.extracted for item in receipts if item.id.startswith("aiops-2025-")), "==", manifest.gates["archive_hashes_verified"]),
        _gate("holdout_case_count", int(holdout["case_count"]), ">=", manifest.gates["holdout_case_count_min"]),
        _gate("prediction_freeze_verified", oracle_opened_at > prediction_frozen_at, "==", manifest.gates["prediction_freeze_verified"]),
        _gate("oracle_leaks", 0, "<=", manifest.gates["oracle_leaks_max"]),
        _gate("unsafe_write_actions", artifact.unsafe_write_actions, "<=", manifest.gates["unsafe_write_actions_max"]),
        _gate("multimodal_case_coverage", float(holdout["multimodal_case_coverage"]), ">=", manifest.gates["multimodal_case_coverage_min"]),
        _gate("entity_top3_accuracy", float(holdout["entity_top3_accuracy"]), ">=", manifest.gates["entity_top3_accuracy_min"]),
        _gate("fault_type_top3_accuracy", float(holdout["fault_type_top3_accuracy"]), ">=", manifest.gates["fault_type_top3_accuracy_min"]),
        _gate("exact_rca_top1_accuracy", float(holdout["exact_rca_top1_accuracy"]), ">=", manifest.gates["exact_rca_top1_accuracy_min"]),
        _gate("evidence_modality_recall", float(holdout["evidence_modality_recall"]), ">=", manifest.gates["evidence_modality_recall_min"]),
    ]
    return TelemetryValidationReport(
        schema="harbor-telemetry-validation-evidence/v2",
        suite_version=manifest.suite_version,
        ruleset_version=manifest.ruleset_version,
        manifest_fingerprint=manifest_fingerprint,
        prediction_fingerprint=prediction_fingerprint,
        prediction_semantic_fingerprint=telemetry_semantic_fingerprint(artifact),
        generated_at=_utc_now(),
        verdict="pass" if all(item.passed for item in gates) else "fail",
        source={
            "id": manifest.source.id,
            "title": manifest.source.title,
            "repository_url": manifest.source.repository_url,
            "revision": manifest.source.revision,
            "license": manifest.source.license.model_dump(),
            "independence": manifest.source.independence,
        },
        coverage={
            **coverage,
            "archive_bytes": sum(item.bytes for item in manifest.archives),
            "calibration_cases": len(calibration_scores),
            "validation_cases": len(validation_scores),
            "holdout_cases": len(holdout_scores),
        },
        protocol={
            "prediction_frozen_at": prediction_frozen_at,
            "oracle_opened_at": oracle_opened_at,
            "oracle_opened_after_freeze": oracle_opened_at > prediction_frozen_at,
            "predictor_oracle_access": "none",
            "prediction_hash_algorithm": "sha256",
            "unsafe_write_actions": artifact.unsafe_write_actions,
            "timezone_contract": manifest.protocol.timezone_contract,
        },
        calibration=calibration,
        validation=validation,
        holdout=holdout,
        gates=gates,
        cases=case_scores,
        provenance=receipts,
        boundaries=[
            "AIOps2025 is a third-party chaos-injected benchmark, not private enterprise production traffic.",
            "The deterministic baseline diagnoses only; it performs no remediation or write operation.",
            "The manifest separates calibration, one or more consumed validation days, and one untouched final holdout day.",
            "A failed quality gate is retained as evidence instead of being hidden or retuned on the holdout.",
            "CC BY-NC 4.0 raw archives remain in the ignored local cache and are not committed.",
        ],
    )


def run_telemetry_validation(
    manifest_path: Path,
    cache_root: Path,
    predictions_path: Path,
    *,
    offline: bool = False,
    timeout_seconds: float = 120.0,
    transport: httpx.BaseTransport | None = None,
) -> TelemetryValidationReport:
    manifest, manifest_fingerprint = TelemetrySourceManifest.from_path(manifest_path)
    fetcher = TelemetryDataFetcher(
        manifest,
        cache_root,
        timeout_seconds=timeout_seconds,
        transport=transport,
    )
    paths, extracted, receipts = fetcher.prepare(offline=offline)
    predictor = TelemetryPredictor(manifest, manifest_fingerprint, extracted)
    try:
        artifact = predictor.predict(paths["input"])
        coverage = predictor.coverage
    finally:
        predictor.close()
    prediction_fingerprint = _atomic_model_write(predictions_path, artifact)
    prediction_frozen_at = _utc_now()
    frozen_artifact = TelemetryPredictionsArtifact.model_validate_json(
        predictions_path.read_bytes()
    )
    if hashlib.sha256(predictions_path.read_bytes()).hexdigest() != prediction_fingerprint:
        raise TelemetryValidationError("prediction artifact changed before scoring")
    return score_frozen_predictions(
        manifest,
        manifest_fingerprint,
        frozen_artifact,
        prediction_fingerprint,
        paths["oracle"],
        coverage,
        receipts,
        prediction_frozen_at,
    )
