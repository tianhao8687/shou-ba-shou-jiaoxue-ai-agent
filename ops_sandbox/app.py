from __future__ import annotations

from contextlib import asynccontextmanager
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import ValidationError
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from backend.app.schemas import Incident
from backend.app.security import (
    AuthenticationError,
    AuthorizationError,
    CapabilityService,
    payload_hash,
)
from backend.app.tools import FAULT_DEFINITIONS, TOOL_REGISTRY


DATABASE_PATH = Path(os.getenv("OPS_DATABASE_PATH", "/data/harbor-fault-lab.db"))
HEALTH_TOKEN = os.getenv("OPS_HEALTH_TOKEN", "harbor-local-health-token")
ORACLE_TOKEN = os.getenv("OPS_ORACLE_TOKEN", "harbor-local-oracle-token-change-me")
CAPABILITY_SECRET = os.getenv(
    "CAPABILITY_SIGNING_SECRET", "harbor-local-capability-secret-change-me"
)
FAULT_INJECTION_ENABLED = os.getenv("OPS_FAULT_INJECTION", "false").lower() == "true"

TOOL_CALLS = Counter(
    "harbor_fault_lab_tool_calls_total",
    "Tool calls committed by the sealed fault lab.",
    ["tool", "status"],
)
IDEMPOTENCY_HITS = Counter(
    "harbor_fault_lab_idempotency_hits_total",
    "Persistent idempotency replays served without another side effect.",
    ["tool"],
)
CAPABILITY_DENIALS = Counter(
    "harbor_fault_lab_capability_denials_total",
    "Capabilities rejected at the tool boundary.",
    ["reason"],
)
EXPERIMENTS = Gauge(
    "harbor_fault_lab_experiments",
    "Experiments currently stored in the sealed lab.",
)


@contextmanager
def _connect():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _initialize() -> None:
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS experiments (
                id TEXT PRIMARY KEY,
                fault_kind TEXT NOT NULL,
                service TEXT NOT NULL,
                state_json TEXT NOT NULL,
                oracle_json TEXT NOT NULL,
                effects INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tool_idempotency (
                idempotency_key TEXT PRIMARY KEY,
                payload_hash TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                status TEXT NOT NULL,
                response_json TEXT NOT NULL,
                capability_jti TEXT NOT NULL,
                job_id TEXT,
                fencing_token INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS capability_usage (
                jti TEXT PRIMARY KEY,
                uses INTEGER NOT NULL,
                max_uses INTEGER NOT NULL,
                last_used_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tool_fencing (
                job_id TEXT PRIMARY KEY,
                max_token INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        # SQLite does not retrofit columns in CREATE TABLE IF NOT EXISTS. Keep
        # existing demo volumes portable across the v3.3 schema upgrade.
        idempotency_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(tool_idempotency)").fetchall()
        }
        if "job_id" not in idempotency_columns:
            connection.execute("ALTER TABLE tool_idempotency ADD COLUMN job_id TEXT")
        if "fencing_token" not in idempotency_columns:
            connection.execute(
                "ALTER TABLE tool_idempotency ADD COLUMN fencing_token INTEGER"
            )


def _bearer(value: str | None) -> str:
    if not value or not value.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    return value.removeprefix("Bearer ").strip()


def _require_static(value: str | None, expected: str) -> None:
    token = _bearer(value)
    if not expected or token != expected:
        raise HTTPException(status_code=403, detail="token is not authorized")


def _incident(experiment_id: str, definition: dict[str, Any]) -> Incident:
    return Incident(
        title=definition["title"],
        summary=definition["summary"],
        severity=definition["severity"],
        service=definition["service"],
        environment="lab",
        symptoms=definition["symptoms"],
        experiment_id=experiment_id,
        tags=["controlled-drill"],
    )


def _apply_tool(
    tool_name: str, payload: dict[str, Any], fault_kind: str, state: dict[str, Any]
) -> tuple[str, str, dict[str, Any], int]:
    if tool_name == "query_metrics":
        output = deepcopy(state["metrics"])
        if "credential_version" in state:
            output["credential_version"] = state["credential_version"]
        if "cache_version" in state:
            output["cache_version"] = state["cache_version"]
            output["db_version"] = state["db_version"]
        return "succeeded", "已读取当前实验指标。", output, 0
    if tool_name == "inspect_logs":
        samples = [
            line.replace("secret=actual", "secret=[REDACTED]")
            for line in state["logs"][: int(payload["limit"])]
        ]
        return "succeeded", "已读取并脱敏日志样本。", {"matches": len(samples), "samples": samples}, 0
    if tool_name == "get_service_status":
        snapshot = {
            "instances": deepcopy(state["instances"]),
            "replicas": state.get("replicas", len(state["instances"])),
            **deepcopy(state.get("metrics", {})),
        }
        for field in (
            "credential_version",
            "cache_version",
            "db_version",
            "partition",
            "applied_checkpoint",
            "expected_checkpoint",
        ):
            if field in state:
                snapshot[field] = deepcopy(state[field])
        return (
            "succeeded",
            "已读取服务实例状态。",
            snapshot,
            0,
        )
    if tool_name == "restart_service":
        instance = payload["instance"]
        state["instances"][instance] = "healthy"
        if fault_kind == "connection_pool_exhaustion" and instance in {
            "checkout-api-1",
            "checkout-api-2",
        }:
            state["metrics"].update({"p95_ms": 540, "error_rate": 0.4, "pool_waiters": 1})
            summary = "实例已重启，连接池等待和错误率恢复。"
        else:
            summary = "重启动作完成，但触发故障的指标没有恢复。"
        return "succeeded", summary, {"instance": instance, **deepcopy(state["metrics"])}, 1
    if tool_name == "scale_workers":
        state["replicas"] = payload["target_replicas"]
        if fault_kind == "queue_backlog" and state["replicas"] >= 5:
            state["metrics"].update(
                {
                    "queue_depth": 3190,
                    "consume_per_min": 690,
                    "oldest_age_s": 280,
                    "trend": "falling",
                }
            )
            summary = "扩容完成，消费速率超过生产速率，积压开始下降。"
        else:
            state["metrics"]["trend"] = "unchanged"
            summary = "扩容动作完成，但目标容量不足或故障并非容量问题。"
        return "succeeded", summary, {"replicas": state["replicas"], **deepcopy(state["metrics"])}, 1
    if tool_name == "rotate_credential":
        state["credential_version"] = payload["target_version"]
        if fault_kind == "expired_credential" and payload["target_version"] == "v18":
            state["metrics"]["http_401_rate"] = 0.3
            summary = "灰度凭据轮换完成，授权失败率恢复。"
        else:
            summary = "轮换动作完成，但授权失败率没有恢复。"
        return (
            "succeeded",
            summary,
            {"credential_version": state["credential_version"], **deepcopy(state["metrics"])},
            1,
        )
    if tool_name == "refresh_cache":
        if fault_kind == "stale_cache" and payload["tenant"] == "xm-retail":
            state["cache_version"] = state["db_version"]
            state["metrics"]["stale_sample_rate"] = 0.0
            summary = "精确缓存刷新完成，样本版本已对齐。"
        else:
            summary = "缓存刷新完成，但目标租户或故障类型不匹配。"
        return (
            "succeeded",
            summary,
            {
                "cache_version": state.get("cache_version"),
                "db_version": state.get("db_version"),
                **deepcopy(state["metrics"]),
            },
            1,
        )
    return "failed", "工具未实现。", {"code": "unsupported_tool"}, 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    _initialize()
    app.state.capabilities = CapabilityService(CAPABILITY_SECRET)
    yield


app = FastAPI(
    title="Harbor Sealed Fault Lab",
    version="3.5.0",
    description="独立故障状态、隐藏真值、签名能力与持久幂等工具边界。",
    lifespan=lifespan,
)


@app.get("/health")
def health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_static(authorization, HEALTH_TOKEN)
    with _connect() as connection:
        experiments = int(connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0])
        idempotency = int(connection.execute("SELECT COUNT(*) FROM tool_idempotency").fetchone()[0])
    return {
        "status": "ready",
        "detail": "persistent sealed fault lab",
        "experiments": experiments,
        "idempotency_records": idempotency,
    }


@app.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    with _connect() as connection:
        EXPERIMENTS.set(
            int(connection.execute("SELECT COUNT(*) FROM experiments").fetchone()[0])
        )
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/v1/lab/fault-kinds")
def fault_kinds() -> list[dict[str, str]]:
    return [
        {"id": key, "service": value["service"], "title": value["title"]}
        for key, value in FAULT_DEFINITIONS.items()
    ]


@app.post("/v1/lab/experiments", status_code=201)
def create_experiment(
    payload: dict[str, Any], authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_static(authorization, ORACLE_TOKEN)
    fault_kind = str(payload.get("fault_kind", ""))
    definition = FAULT_DEFINITIONS.get(fault_kind)
    if definition is None:
        raise HTTPException(status_code=422, detail="unknown fault_kind")
    experiment_id = f"EXP-{uuid4().hex[:12].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO experiments(id,fault_kind,service,state_json,oracle_json,effects,created_at)
            VALUES (?,?,?,?,?,0,?)
            """,
            (
                experiment_id,
                fault_kind,
                definition["service"],
                json.dumps(definition["state"], ensure_ascii=False),
                json.dumps(definition["oracle"], ensure_ascii=False),
                now,
            ),
        )
    return {
        "experiment_id": experiment_id,
        "incident": _incident(experiment_id, definition).model_dump(mode="json"),
    }


@app.get("/v1/lab/experiments/{experiment_id}/oracle")
def oracle(
    experiment_id: str, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    _require_static(authorization, ORACLE_TOKEN)
    with _connect() as connection:
        row = connection.execute(
            "SELECT fault_kind,oracle_json,effects FROM experiments WHERE id=?", (experiment_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="experiment not found")
    return {
        "experiment_id": experiment_id,
        "fault_kind": row["fault_kind"],
        "oracle": json.loads(row["oracle_json"]),
        "effects": int(row["effects"]),
    }


@app.post("/v1/tools/{tool_name}")
def invoke_tool(
    tool_name: str,
    payload: dict[str, Any],
    request: Request,
    authorization: str | None = Header(default=None),
    x_idempotency_key: str | None = Header(default=None),
    x_harbor_control_tenant: str = Header(...),
    x_harbor_job_id: str = Header(...),
    x_harbor_fencing_token: int = Header(...),
    x_test_drop_response: str | None = Header(default=None),
) -> dict[str, Any]:
    spec = TOOL_REGISTRY.get(tool_name)
    if spec is None:
        raise HTTPException(status_code=404, detail="tool not found")
    if not x_idempotency_key or len(x_idempotency_key) != 64:
        raise HTTPException(status_code=422, detail="a 64-character X-Idempotency-Key is required")
    if not x_harbor_job_id.strip() or len(x_harbor_job_id) > 100:
        raise HTTPException(status_code=422, detail="a valid X-Harbor-Job-Id is required")
    if x_harbor_fencing_token < 1:
        raise HTTPException(status_code=422, detail="X-Harbor-Fencing-Token must be positive")
    try:
        validated = spec.input_model.model_validate(payload).model_dump()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    token = _bearer(authorization)
    try:
        claims = request.app.state.capabilities.verify(
            token,
            tool_name=tool_name,
            payload=validated,
            tenant_id=x_harbor_control_tenant,
            job_id=x_harbor_job_id,
            fencing_token=x_harbor_fencing_token,
        )
    except AuthenticationError as exc:
        CAPABILITY_DENIALS.labels(reason="authentication").inc()
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except AuthorizationError as exc:
        CAPABILITY_DENIALS.labels(reason="authorization").inc()
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    current_payload_hash = payload_hash(validated)
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        fence = connection.execute(
            "SELECT max_token FROM tool_fencing WHERE job_id=?", (x_harbor_job_id,)
        ).fetchone()
        if fence is not None and x_harbor_fencing_token < int(fence["max_token"]):
            CAPABILITY_DENIALS.labels(reason="stale_fencing_token").inc()
            raise HTTPException(
                status_code=409,
                detail=(
                    f"stale fencing token {x_harbor_fencing_token}; "
                    f"latest is {int(fence['max_token'])}"
                ),
            )
        connection.execute(
            """
            INSERT INTO tool_fencing(job_id,max_token,updated_at) VALUES (?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
                max_token=MAX(tool_fencing.max_token,excluded.max_token),
                updated_at=excluded.updated_at
            """,
            (x_harbor_job_id, x_harbor_fencing_token, now),
        )
        prior = connection.execute(
            "SELECT payload_hash,tool_name,response_json FROM tool_idempotency WHERE idempotency_key=?",
            (x_idempotency_key,),
        ).fetchone()
        if prior is not None:
            if prior["payload_hash"] != current_payload_hash or prior["tool_name"] != tool_name:
                raise HTTPException(
                    status_code=409, detail="idempotency key was already used for different input"
                )
            original = json.loads(prior["response_json"])
            original["status"] = "skipped"
            original["summary"] = "持久幂等命中：返回首次提交结果。"
            IDEMPOTENCY_HITS.labels(tool=tool_name).inc()
            TOOL_CALLS.labels(tool=tool_name, status="skipped").inc()
            return original

        usage = connection.execute(
            "SELECT uses,max_uses FROM capability_usage WHERE jti=?", (claims["jti"],)
        ).fetchone()
        uses = int(usage["uses"]) if usage else 0
        max_uses = int(claims.get("max_uses", 1))
        if uses >= max_uses:
            raise HTTPException(status_code=403, detail="capability replay limit exceeded")
        experiment = connection.execute(
            "SELECT fault_kind,service,state_json,effects FROM experiments WHERE id=?",
            (validated["experiment_id"],),
        ).fetchone()
        if experiment is None:
            raise HTTPException(status_code=404, detail="experiment not found")
        if experiment["service"] != validated["service"]:
            raise HTTPException(status_code=409, detail="service does not match experiment")
        state = json.loads(experiment["state_json"])
        status, summary, output, effect_delta = _apply_tool(
            tool_name, validated, experiment["fault_kind"], state
        )
        response = {"status": status, "summary": summary, "output": output, "error": None}
        TOOL_CALLS.labels(tool=tool_name, status=status).inc()
        connection.execute(
            "UPDATE experiments SET state_json=?,effects=effects+? WHERE id=?",
            (json.dumps(state, ensure_ascii=False), effect_delta, validated["experiment_id"]),
        )
        connection.execute(
            """
            INSERT INTO tool_idempotency(
                idempotency_key,payload_hash,tool_name,status,response_json,capability_jti,
                job_id,fencing_token,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                x_idempotency_key,
                current_payload_hash,
                tool_name,
                status,
                json.dumps(response, ensure_ascii=False),
                claims["jti"],
                x_harbor_job_id,
                x_harbor_fencing_token,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO capability_usage(jti,uses,max_uses,last_used_at) VALUES (?,1,?,?)
            ON CONFLICT(jti) DO UPDATE SET uses=uses+1,last_used_at=excluded.last_used_at
            """,
            (claims["jti"], max_uses, now),
        )
    if x_test_drop_response == "after-commit" and FAULT_INJECTION_ENABLED:
        raise HTTPException(status_code=503, detail="injected response loss after durable commit")
    return response
