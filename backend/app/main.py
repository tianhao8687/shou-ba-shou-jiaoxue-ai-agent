from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import Settings, get_settings
from .evaluation import EvaluationService
from .external_validation import ExternalValidationReport, load_external_report
from .telemetry_validation import TelemetryValidationReport, load_telemetry_report
from .metrics import build_metrics
from .observability import CONCURRENCY_CONFLICTS
from .runtime import build_agent_runtime
from .schemas import (
    AuthResponse,
    DashboardMetrics,
    DecisionRequest,
    DrillDescriptor,
    DrillRequest,
    EvaluationReport,
    HealthResponse,
    JobRecord,
    LoginRequest,
    RunRecord,
    StartRunRequest,
    UserIdentity,
)
from .security import (
    AuthenticationError,
    AuthService,
    AuthorizationError,
    require_any_role,
)
from .store import ConcurrencyError, Store
from .tools import FAULT_DEFINITIONS, TOOL_REGISTRY


APP_VERSION = "3.6.0"


def _token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer access token required")
    return authorization.removeprefix("Bearer ").strip()


def current_user(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> UserIdentity:
    try:
        return request.app.state.auth.verify(_token(authorization))
    except HTTPException:
        raise
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _authorize(user: UserIdentity, roles: list[str]) -> None:
    try:
        require_any_role(user, roles)
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _tenant_run(store: Store, run_id: str, user: UserIdentity) -> RunRecord:
    run = store.get_run(run_id)
    if run is None or run.tenant_id != user.tenant_id:
        # Deliberately do not reveal that another tenant owns this identifier.
        raise HTTPException(status_code=404, detail="运行不存在")
    return run


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime = build_agent_runtime(resolved)
        store = runtime.store
        retriever = runtime.retriever
        auth = AuthService(
            resolved.auth_signing_secret,
            resolved.demo_password,
            resolved.auth_ttl_minutes,
        )
        capabilities = runtime.capabilities
        model_adapter = runtime.model_adapter
        tool_executor = runtime.tool_executor
        inprocess_lab = runtime.inprocess_lab
        engine = runtime.engine
        worker = runtime.worker
        evaluator = EvaluationService(
            resolved.data_dir / "evaluation" / "manifest.json",
            retriever,
            resolved.capability_signing_secret,
            model_adapter,
        )
        external_validation = load_external_report(
            resolved.data_dir / "external" / "evidence-v1.json"
        )
        telemetry_validation = load_telemetry_report(
            resolved.data_dir / "external" / "telemetry-evidence-v4.json"
        )
        app.state.settings = resolved
        app.state.store = store
        app.state.retriever = retriever
        app.state.auth = auth
        app.state.capabilities = capabilities
        app.state.engine = engine
        app.state.worker = worker
        app.state.evaluator = evaluator
        app.state.external_validation = external_validation
        app.state.telemetry_validation = telemetry_validation
        app.state.model_adapter = model_adapter
        app.state.tool_executor = tool_executor
        app.state.inprocess_lab = inprocess_lab
        if resolved.embedded_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(
        title=resolved.app_name,
        version=APP_VERSION,
        description="证据约束、可恢复、最小权限并由隐藏真值验证的 AIOps Agent。",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"] ,
        allow_headers=["*"] ,
    )

    @app.get("/api", tags=["system"])
    def api_index() -> dict[str, Any]:
        return {
            "name": resolved.app_name,
            "version": APP_VERSION,
            "docs": "/docs",
            "runtime_contract": "freeform incident + sealed oracle + durable worker",
        }

    @app.post("/api/auth/login", response_model=AuthResponse, tags=["auth"])
    def login(payload: LoginRequest, request: Request) -> AuthResponse:
        try:
            return request.app.state.auth.authenticate(payload.username, payload.password)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @app.get("/api/auth/me", response_model=UserIdentity, tags=["auth"])
    def me(user: Annotated[UserIdentity, Depends(current_user)]) -> UserIdentity:
        return user

    def runtime_status(
        request: Request, *, purpose: Literal["runtime-status", "readiness"]
    ) -> HealthResponse:
        store_name = "postgresql" if resolved.database_url.startswith("postgres") else "sqlite"
        try:
            request.app.state.store.ping()
            schema_status = request.app.state.store.schema_status()
            database_status = {"status": "ready", "schema": schema_status}
            database_ready = schema_status.get("status") == "current"
        except Exception as exc:
            database_status = {
                "status": "unavailable",
                "detail": f"{type(exc).__name__}: durable store query failed",
            }
            database_ready = False
        vector_quality = request.app.state.retriever.vector_quality
        vector_ready = vector_quality != "unavailable"
        model_runtime = request.app.state.model_adapter.health()
        if resolved.model_fixture_mode:
            model_ready = model_runtime.get("status") == "fixture"
            model_runtime["production_capable"] = False
            model_runtime["readiness_contract"] = "explicit-fixture-mode"
        elif resolved.model_enabled:
            model_ready = (
                model_runtime.get("status") == "ready"
                and model_runtime.get("loaded") is True
            )
            model_runtime["production_capable"] = model_ready
            model_runtime["readiness_contract"] = "live-model-must-be-loaded"
        else:
            model_ready = False
            model_runtime["production_capable"] = False
            model_runtime["readiness_contract"] = "no-model-configured"
        tool_runtime = request.app.state.tool_executor.client.health()
        tool_ready = tool_runtime.get("status") == "ready"
        worker_runtime = request.app.state.worker.health()
        worker_ready = worker_runtime.get("status") == "running"
        if not resolved.embedded_worker:
            jobs = request.app.state.store.list_jobs() if database_ready else []
            live_workers = (
                request.app.state.store.list_live_workers(
                    resolved.worker_registry_ttl_seconds
                )
                if database_ready
                else []
            )
            now = datetime.now(timezone.utc)
            active_leases = sum(
                1
                for job in jobs
                if job.status == "claimed" and job.lease_until and job.lease_until > now
            )
            worker_runtime = {
                "status": "external",
                "mode": "standalone-process",
                "required_for_api_readiness": False,
                "verified": bool(live_workers),
                "delivery_status": "ready" if live_workers else "unavailable",
                "registered_workers": [
                    {
                        **worker,
                        "started_at": worker["started_at"].isoformat(),
                        "last_seen_at": worker["last_seen_at"].isoformat(),
                    }
                    for worker in live_workers
                ],
                "registered_worker_count": len(live_workers),
                "registry_ttl_seconds": resolved.worker_registry_ttl_seconds,
                "active_leases": active_leases,
                "queued_jobs": sum(1 for job in jobs if job.status == "queued"),
                "detail": (
                    "Durable database heartbeats verify idle and busy worker processes; "
                    "job leases and fencing still govern execution ownership."
                ),
            }
            # The API only accepts into a durable queue; a standalone worker is a
            # delivery-SLO dependency, not a precondition for accepting the request.
            worker_ready = True
        readiness_checks = {
            "database": database_ready,
            "retrieval": vector_ready,
            "model_contract": model_ready,
            "tool_boundary": tool_ready,
            "api_queue_acceptance": worker_ready,
        }
        ready = all(readiness_checks.values())
        return HealthResponse(
            status="ready" if ready else "not_ready",
            ready=ready,
            purpose=purpose,
            app=resolved.app_name,
            version=APP_VERSION,
            mode=resolved.app_env,
            database=store_name,
            database_status=database_status,
            vector_backend=request.app.state.retriever.backend_name,
            vector_quality=vector_quality,
            knowledge_documents=len(request.app.state.retriever.documents),
            knowledge_chunks=request.app.state.retriever.chunk_count,
            model_runtime=model_runtime,
            tool_runtime=tool_runtime,
            worker_runtime=worker_runtime,
            readiness_checks=readiness_checks,
        )

    @app.get("/api/health", tags=["system"])
    def health() -> dict[str, str]:
        """Process liveness only; never claims that downstream dependencies are ready."""
        return {"status": "alive", "app": resolved.app_name, "version": APP_VERSION}

    @app.get("/api/status", response_model=HealthResponse, tags=["system"])
    def status(request: Request) -> HealthResponse:
        """Detailed diagnostic status for the UI; always returns an inspectable body."""
        return runtime_status(request, purpose="runtime-status")

    @app.get("/api/ready", tags=["system"])
    def ready(request: Request) -> JSONResponse:
        """Traffic readiness; returns 503 whenever a required contract is unavailable."""
        report = runtime_status(request, purpose="readiness")
        return JSONResponse(
            status_code=200 if report.ready else 503,
            content=report.model_dump(mode="json"),
        )

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/drills", tags=["drills"])
    def drills(user: Annotated[UserIdentity, Depends(current_user)]) -> list[dict[str, str]]:
        _authorize(user, ["observer"])
        return [
            {"id": key, "service": value["service"], "title": value["title"]}
            for key, value in FAULT_DEFINITIONS.items()
        ]

    @app.post("/api/drills", response_model=DrillDescriptor, status_code=201, tags=["drills"])
    def create_drill(
        payload: DrillRequest,
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> DrillDescriptor:
        _authorize(user, ["admin"])
        lab = request.app.state.inprocess_lab
        if lab is not None:
            try:
                experiment_id, incident = lab.create_experiment(payload.fault_kind)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="fault kind is not registered") from exc
            return DrillDescriptor(experiment_id=experiment_id, incident=incident)
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.post(
                    f"{resolved.tool_sandbox_url.rstrip('/')}/v1/lab/experiments",
                    headers={"Authorization": f"Bearer {resolved.lab_oracle_token}"},
                    json={"fault_kind": payload.fault_kind},
                )
                response.raise_for_status()
                body = response.json()
            return DrillDescriptor.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"fault lab unavailable: {type(exc).__name__}") from exc

    @app.get("/api/runs", response_model=list[RunRecord], tags=["runs"])
    def runs(
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[RunRecord]:
        _authorize(user, ["observer"])
        return request.app.state.store.list_runs(user.tenant_id, limit)

    @app.get("/api/runs/{run_id}", response_model=RunRecord, tags=["runs"])
    def run_detail(
        run_id: str,
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> RunRecord:
        _authorize(user, ["observer"])
        return _tenant_run(request.app.state.store, run_id, user)

    @app.get("/api/runs/{run_id}/jobs", response_model=list[JobRecord], tags=["runs"])
    def run_jobs(
        run_id: str,
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> list[JobRecord]:
        _authorize(user, ["observer"])
        _tenant_run(request.app.state.store, run_id, user)
        return request.app.state.store.list_jobs(run_id)

    @app.post("/api/runs", response_model=RunRecord, status_code=202, tags=["runs"])
    def start_run(
        payload: StartRunRequest,
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> RunRecord:
        _authorize(user, ["operator"])
        return request.app.state.engine.start(payload.incident, user)

    @app.post("/api/runs/{run_id}/decision", response_model=RunRecord, tags=["runs"])
    def decide(
        run_id: str,
        payload: DecisionRequest,
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> RunRecord:
        _tenant_run(request.app.state.store, run_id, user)
        try:
            return request.app.state.engine.decide(
                run_id,
                payload.decision,
                user,
                payload.note,
                payload.expected_version,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValueError, ConcurrencyError) as exc:
            if isinstance(exc, ConcurrencyError):
                CONCURRENCY_CONFLICTS.inc()
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/retry", response_model=RunRecord, tags=["runs"])
    def retry(
        run_id: str,
        expected_version: int,
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> RunRecord:
        _authorize(user, ["on-call-lead"])
        _tenant_run(request.app.state.store, run_id, user)
        try:
            return request.app.state.engine.retry_failed(
                run_id, user, expected_version
            )
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (KeyError, ValueError, ConcurrencyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/cancel", response_model=RunRecord, tags=["runs"])
    def cancel(
        run_id: str,
        expected_version: int,
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> RunRecord:
        _authorize(user, ["operator"])
        _tenant_run(request.app.state.store, run_id, user)
        try:
            return request.app.state.engine.cancel(run_id, user, expected_version)
        except AuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (KeyError, ValueError, ConcurrencyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/evidence", tags=["runs"])
    def evidence_bundle(
        run_id: str,
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> dict[str, Any]:
        _authorize(user, ["observer"])
        run = _tenant_run(request.app.state.store, run_id, user)
        raw = run.model_dump(mode="json")
        forbidden = ["expected_cause", "expected_resolution", "expected_plan"]
        serialized = json.dumps(raw, ensure_ascii=False)
        return {
            "schema": "harbor-evidence-bundle/v3",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "run": raw,
            "jobs": [job.model_dump(mode="json") for job in request.app.state.store.list_jobs(run_id)],
            "runtime_leak_check": {
                "forbidden_fields": forbidden,
                "hits": [field for field in forbidden if field in serialized],
                "passed": not any(field in serialized for field in forbidden),
            },
            "contract": {
                "model_mode": run.run_mode,
                "vector_quality": request.app.state.retriever.vector_quality,
                "tool_transport": request.app.state.tool_executor.client.name,
                "unsafe_action_policy": "fail-closed",
            },
        }

    @app.get("/api/knowledge", tags=["knowledge"])
    def knowledge(
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> list[dict[str, Any]]:
        _authorize(user, ["observer"])
        return [
            {
                "id": document.doc_id,
                "title": document.title,
                "section": document.section,
                "uri": document.uri,
                "characters": len(document.content),
                "preview": document.content.replace("\n", " ")[:180],
            }
            for document in request.app.state.retriever.documents
        ]

    @app.get("/api/tools", tags=["system"])
    def tools(user: Annotated[UserIdentity, Depends(current_user)]) -> list[dict[str, Any]]:
        _authorize(user, ["observer"])
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "risk": spec.risk,
                "required_role": spec.required_role,
                "read_only": spec.read_only,
                "applicability": spec.applicability,
                "input_schema": spec.input_model.model_json_schema(),
            }
            for spec in TOOL_REGISTRY.values()
        ]

    @app.get("/api/metrics", response_model=DashboardMetrics, tags=["metrics"])
    def metrics(
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> DashboardMetrics:
        _authorize(user, ["observer"])
        store: Store = request.app.state.store
        tenant_runs = store.list_runs(user.tenant_id)
        return build_metrics(tenant_runs, store.latest_evaluation(user.tenant_id))

    @app.get(
        "/api/evaluations/latest",
        response_model=EvaluationReport | None,
        tags=["evaluations"],
    )
    def latest_evaluation(
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> EvaluationReport | None:
        _authorize(user, ["observer"])
        return request.app.state.store.latest_evaluation(user.tenant_id)

    @app.get(
        "/api/evaluations/external/latest",
        response_model=ExternalValidationReport | None,
        tags=["evaluations"],
    )
    def latest_external_validation(
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> ExternalValidationReport | None:
        _authorize(user, ["observer"])
        return request.app.state.external_validation

    @app.get(
        "/api/evaluations/telemetry/latest",
        response_model=TelemetryValidationReport | None,
        tags=["evaluations"],
    )
    def latest_telemetry_validation(
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
    ) -> TelemetryValidationReport | None:
        _authorize(user, ["observer"])
        return request.app.state.telemetry_validation

    @app.post("/api/evaluations/run", response_model=EvaluationReport, tags=["evaluations"])
    def run_evaluation(
        request: Request,
        user: Annotated[UserIdentity, Depends(current_user)],
        live_model: bool = Query(default=False),
        case_limit: int | None = Query(default=None, ge=1, le=120),
        case_offset: int = Query(default=0, ge=0, le=119),
        split: str = Query(default="development", pattern="^(calibration|development|holdout)$"),
        final_holdout: bool = Query(default=False),
    ) -> EvaluationReport:
        _authorize(user, ["admin"])
        report = request.app.state.evaluator.run(
            tenant_id=user.tenant_id,
            live_model=live_model,
            case_limit=case_limit,
            case_offset=case_offset,
            split=split,
            allow_frozen_holdout=final_holdout,
        )
        return request.app.state.store.save_evaluation(report)

    return app


app = create_app()
