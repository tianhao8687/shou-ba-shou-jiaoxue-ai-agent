from __future__ import annotations

from ..model_adapter import ModelAdapter
from ..policy import PolicyCompiler
from ..retrieval import Retriever
from ..store import Store
from ..tools import ToolExecutor
from .approvals import ApprovalMixin
from .context import NodeHandler
from .lifecycle import LifecycleMixin
from .nodes import (
    diagnose,
    execute,
    finalize,
    gate,
    intake,
    investigate,
    observe,
    policy as policy_node,
    retrieve,
    verify,
)
from .transitions import DEFAULT_WORKFLOW_TIMEOUT_SECONDS


class AgentEngine(ApprovalMixin, LifecycleMixin):
    """Compose dependencies and the explicit workflow node table.

    Orchestration lives in ``LifecycleMixin`` and transition validation in
    ``transitions.py``.  This class intentionally contains no node business logic.
    """

    def __init__(
        self,
        store: Store,
        retriever: Retriever,
        tool_executor: ToolExecutor,
        model_adapter: ModelAdapter,
        policy: PolicyCompiler | None = None,
        *,
        medium_risk_approval_quorum: int = 1,
        high_risk_approval_quorum: int = 2,
        enforce_requester_separation: bool = True,
        run_mode: str = "live-model",
        production_observation_enabled: bool = False,
        kubernetes_connector_enabled: bool = False,
        kubernetes_staging_namespace: str = "harbor-sandbox",
        workflow_timeout_seconds: int = DEFAULT_WORKFLOW_TIMEOUT_SECONDS,
    ) -> None:
        self.store = store
        self.retriever = retriever
        self.tool_executor = tool_executor
        self.model_adapter = model_adapter
        self.policy_compiler = policy or PolicyCompiler()
        if not 1 <= medium_risk_approval_quorum <= 4:
            raise ValueError("medium-risk approval quorum must be between 1 and 4")
        if not 1 <= high_risk_approval_quorum <= 4:
            raise ValueError("high-risk approval quorum must be between 1 and 4")
        if workflow_timeout_seconds <= 0:
            raise ValueError("workflow timeout must be positive")
        self.medium_risk_approval_quorum = medium_risk_approval_quorum
        self.high_risk_approval_quorum = high_risk_approval_quorum
        self.enforce_requester_separation = enforce_requester_separation
        self.run_mode = run_mode
        self.production_observation_enabled = production_observation_enabled
        self.kubernetes_connector_enabled = kubernetes_connector_enabled
        self.kubernetes_staging_namespace = kubernetes_staging_namespace
        self.workflow_timeout_seconds = workflow_timeout_seconds
        self.node_handlers: dict[str, NodeHandler] = {
            "intake": intake.run,
            "retrieve": retrieve.run,
            "investigate": investigate.run,
            "observe": observe.run,
            "diagnose": diagnose.run,
            "policy": policy_node.run,
            "gate": gate.run,
            "execute": execute.run,
            "verify": verify.run,
            "finalize": finalize.run,
        }


# Compatibility methods for internal callers that previously used helpers on the
# monolithic engine.  New node code imports the focused functions directly.
AgentEngine._baseline_investigation_plan = investigate.baseline_plan
AgentEngine._execution_actor = staticmethod(execute.execution_actor)
AgentEngine._execute_automatic_rollback = verify.execute_automatic_rollback
