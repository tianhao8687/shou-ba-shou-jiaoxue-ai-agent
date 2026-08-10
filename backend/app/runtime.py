from __future__ import annotations

from dataclasses import dataclass

from .agent import AgentEngine
from .config import Settings
from .model_adapter import (
    CoordinatedModelAdapter,
    HeuristicModelAdapter,
    ModelAdapter,
    create_model_adapter,
)
from .policy import PolicyCompiler
from .retrieval import Retriever, create_retriever
from .security import CapabilityService
from .store import Store, create_store
from .tools import InMemoryFaultLabClient, ToolExecutor, create_tool_executor
from .worker import AgentWorker


@dataclass(frozen=True)
class AgentRuntime:
    store: Store
    retriever: Retriever
    capabilities: CapabilityService
    model_adapter: ModelAdapter
    tool_executor: ToolExecutor
    inprocess_lab: InMemoryFaultLabClient | None
    engine: AgentEngine
    worker: AgentWorker


def build_agent_runtime(settings: Settings, *, worker_id: str | None = None) -> AgentRuntime:
    """Build the same execution boundary for API-embedded and standalone workers."""

    store = create_store(settings.database_url)
    store.initialize()
    retriever = create_retriever(
        settings.data_dir,
        settings.vector_backend,
        settings.database_url,
        settings.embedding_backend,
        settings.embedding_endpoint,
        settings.embedding_model,
        settings.embedding_api_key,
    )
    capabilities = CapabilityService(
        settings.capability_signing_secret, settings.capability_ttl_seconds
    )
    uncoordinated_model_adapter: ModelAdapter = (
        HeuristicModelAdapter("MODEL_FIXTURE_MODE=true")
        if settings.model_fixture_mode
        else create_model_adapter(
            enabled=settings.model_enabled,
            endpoint=settings.model_endpoint,
            model=settings.model_name,
            provider=settings.model_provider,
            timeout_seconds=settings.model_timeout_seconds,
            api_key=settings.model_api_key,
        )
    )
    model_adapter: ModelAdapter = CoordinatedModelAdapter(
        uncoordinated_model_adapter,
        store.model_inference_slot,
        (
            "postgresql-advisory-lock"
            if settings.database_url.startswith(("postgres://", "postgresql://"))
            else "process-lock"
        ),
    )
    tool_executor, inprocess_lab = create_tool_executor(
        mode=settings.tool_mode,
        base_url=settings.tool_sandbox_url,
        health_token=settings.tool_sandbox_token,
        timeout_seconds=settings.tool_timeout_seconds,
        capabilities=capabilities,
        prometheus_url=settings.prometheus_url,
        prometheus_bearer_token=settings.prometheus_bearer_token,
        prometheus_timeout_seconds=settings.prometheus_timeout_seconds,
    )
    engine = AgentEngine(
        store,
        retriever,
        tool_executor,
        model_adapter,
        PolicyCompiler(),
        medium_risk_approval_quorum=settings.medium_risk_approval_quorum,
        high_risk_approval_quorum=settings.high_risk_approval_quorum,
        enforce_requester_separation=settings.enforce_requester_separation,
        run_mode=(
            "test-fixture"
            if settings.model_fixture_mode
            else "live-model"
            if settings.model_enabled
            else "model-degraded"
        ),
        production_observation_enabled=bool(settings.prometheus_url),
    )
    worker = AgentWorker(
        store,
        engine,
        worker_id=worker_id or settings.worker_id,
        lease_seconds=settings.worker_lease_seconds,
        heartbeat_seconds=settings.worker_heartbeat_seconds,
        heartbeat_failure_limit=settings.worker_heartbeat_failure_limit,
        poll_seconds=settings.worker_poll_seconds,
    )
    return AgentRuntime(
        store=store,
        retriever=retriever,
        capabilities=capabilities,
        model_adapter=model_adapter,
        tool_executor=tool_executor,
        inprocess_lab=inprocess_lab,
        engine=engine,
        worker=worker,
    )
