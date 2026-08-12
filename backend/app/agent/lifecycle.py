from __future__ import annotations

import time
from typing import Any, Callable
from uuid import uuid4

from ..schemas import (
    Incident,
    RunRecord,
    RunStatus,
    TraceStatus,
    TraceStep,
    UserIdentity,
    WorkerLeaseEvent,
)
from ..security import AuthorizationError, require_all_roles
from ..store import ConcurrencyError, LeaseLostError
from ..store.audit import append_audit_event
from .context import LeaseContext, NODE_LABELS, NodeHandler
from .transitions import (
    DEFAULT_WORKFLOW_TIMEOUT_SECONDS,
    InvalidTransitionError,
    NodeRetryBudgetExceeded,
    WorkflowDeadlineExceeded,
    apply_transition,
    assert_within_deadline,
    consume_node_attempt,
    ensure_deadline,
    remaining_node_attempts,
)


TERMINAL_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.HANDED_OFF,
    RunStatus.CANCELLED,
}


class LifecycleMixin:
    store: Any
    node_handlers: dict[str, NodeHandler]
    run_mode: str
    workflow_timeout_seconds: int

    def start(
        self,
        incident: Incident,
        actor: UserIdentity | str = "requester",
    ) -> RunRecord:
        actor_name = actor.username if isinstance(actor, UserIdentity) else actor
        tenant_id = actor.tenant_id if isinstance(actor, UserIdentity) else "xm-ops"
        run = RunRecord(
            id=f"RUN-{uuid4().hex[:12].upper()}",
            incident=incident,
            tenant_id=tenant_id,
            created_by=actor_name,
            status=RunStatus.QUEUED,
            current_node="intake",
            run_mode=self.run_mode,
        )
        append_audit_event(
            run,
            actor_name,
            "run.created",
            "自由文本事件已保存并进入持久任务队列。",
            {
                "has_experiment": bool(incident.experiment_id),
                "tenant_id": tenant_id,
            },
        )
        ensure_deadline(run, self.workflow_timeout_seconds)
        self.store.save_run_and_enqueue(run, "start")
        return run

    def process(self, run_id: str, lease: LeaseContext) -> RunRecord:
        self.assert_lease(lease)
        run = self.require_run(run_id)
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.HANDED_OFF,
        }:
            return run
        ensure_deadline(run, self.workflow_timeout_seconds)
        if run.cancellation_requested:
            self.transition(
                run,
                "cancelled",
                lease,
                actor="system",
                reason="取消请求在节点执行前生效。",
                status=RunStatus.CANCELLED,
            )
            return run

        run.status = RunStatus.RUNNING
        run.attempt += 1
        run.lease_history.append(
            WorkerLeaseEvent(
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                action="recovered" if lease.recovered else "claimed",
            )
        )
        self.audit(
            run,
            lease.worker_id,
            "job.recovered" if lease.recovered else "job.claimed",
            f"worker 使用 fencing token {lease.fencing_token} 处理任务。",
            {"job_id": lease.job_id},
        )
        self.checkpoint(run, lease)

        while run.status == RunStatus.RUNNING:
            try:
                assert_within_deadline(run)
            except WorkflowDeadlineExceeded as exc:
                self.fail_run(run, exc)
                break
            if run.cancellation_requested:
                self.transition(
                    run,
                    "cancelled",
                    lease,
                    actor="system",
                    reason="取消请求在节点边界生效。",
                    status=RunStatus.CANCELLED,
                )
                break
            node = run.current_node
            handler = self.node_handlers.get(node)
            if handler is None:
                self.fail_run(run, InvalidTransitionError(f"unknown workflow node: {node}"))
                break
            self.run_node(run, node, handler, lease)
            if run.status in TERMINAL_STATUSES | {RunStatus.AWAITING_APPROVAL}:
                break
        return self.checkpoint(run, lease)

    def run_node(
        self,
        run: RunRecord,
        node: str,
        handler: NodeHandler,
        lease: LeaseContext,
    ) -> None:
        self.assert_lease(lease)
        started = time.perf_counter()
        trace = TraceStep(
            id=f"TR-{uuid4().hex[:10].upper()}",
            node=node,
            label=NODE_LABELS.get(node, node),
            status=TraceStatus.RUNNING,
            summary="节点开始执行。",
        )
        outcome = None
        try:
            attempt = consume_node_attempt(run, node)
            trace.metadata["node_attempt"] = attempt
            outcome = handler(self, run, lease)
            self.assert_lease(lease)
            trace.status = (
                TraceStatus.WAITING
                if outcome.status == RunStatus.AWAITING_APPROVAL
                else TraceStatus.BLOCKED
                if outcome.status == RunStatus.HANDED_OFF
                else TraceStatus.FAILED
                if outcome.status == RunStatus.FAILED
                else TraceStatus.COMPLETED
            )
            trace.summary = outcome.summary
            trace.output_preview = outcome.output[:500]
        except LeaseLostError:
            raise
        except Exception as exc:
            trace.status = TraceStatus.FAILED
            trace.summary = f"{type(exc).__name__}: {str(exc)[:240]}"
            self.fail_run(run, exc)
            self.audit(run, "system", "node.failed", trace.summary, {"node": node})
        finally:
            trace.duration_ms = max(1, round((time.perf_counter() - started) * 1000))
            run.traces.append(trace)
            self.checkpoint(run, lease)

        if outcome is None or run.status == RunStatus.FAILED:
            return
        if outcome.next_node is None:
            if outcome.status is None:
                self.fail_run(
                    run,
                    InvalidTransitionError(
                        f"node {node} returned neither next_node nor terminal status"
                    ),
                )
                self.checkpoint(run, lease)
                return
            run.status = outcome.status
            self.audit(
                run,
                "workflow",
                "workflow.status",
                outcome.reason or outcome.summary,
                {"node": node, "status": str(outcome.status)},
            )
            self.checkpoint(run, lease)
            return
        self.transition(
            run,
            outcome.next_node,
            lease,
            actor="workflow",
            reason=outcome.reason or outcome.summary,
            status=outcome.status,
        )

    def transition(
        self,
        run: RunRecord,
        target: str,
        lease: LeaseContext,
        *,
        actor: str,
        reason: str,
        status: RunStatus | None = None,
    ) -> RunRecord:
        assert_within_deadline(run)
        apply_transition(run, target, actor=actor, reason=reason, status=status)
        return self.checkpoint(run, lease)

    @staticmethod
    def fail_run(run: RunRecord, exc: Exception) -> None:
        run.status = RunStatus.FAILED
        if isinstance(exc, WorkflowDeadlineExceeded):
            run.error_code = "WORKFLOW_DEADLINE_EXCEEDED"
        elif isinstance(exc, NodeRetryBudgetExceeded):
            run.error_code = "NODE_RETRY_BUDGET_EXCEEDED"
        elif isinstance(exc, InvalidTransitionError):
            run.error_code = "INVALID_STATE_TRANSITION"
        else:
            run.error_code = type(exc).__name__.upper()
        run.error_detail = str(exc)[:1000]

    def cancel(self, run_id: str, user: UserIdentity, expected_version: int) -> RunRecord:
        run = self.require_run(run_id)
        if run.tenant_id != user.tenant_id:
            raise AuthorizationError("run does not belong to the authenticated tenant")
        if run.version != expected_version:
            raise ConcurrencyError(
                f"运行 {run.id} 版本冲突：期望 {expected_version}，当前 {run.version}"
            )
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.HANDED_OFF,
        }:
            raise ValueError("终态运行不能取消")
        run.cancellation_requested = True
        if run.status in {
            RunStatus.QUEUED,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.FAILED,
        }:
            apply_transition(
                run,
                "cancelled",
                actor=user.username,
                reason="用户在非运行节点请求取消。",
                status=RunStatus.CANCELLED,
            )
        self.audit(run, user.username, "run.cancel_requested", "用户请求取消运行。")
        return self.store.save_run(run)

    def retry_failed(
        self, run_id: str, user: UserIdentity, expected_version: int
    ) -> RunRecord:
        run = self.require_run(run_id)
        if run.tenant_id != user.tenant_id:
            raise AuthorizationError("run does not belong to the authenticated tenant")
        if run.version != expected_version:
            raise ConcurrencyError(
                f"运行 {run.id} 版本冲突：期望 {expected_version}，当前 {run.version}"
            )
        if run.status != RunStatus.FAILED:
            raise ValueError("只有失败运行可以重试")
        if run.attempt >= run.max_attempts:
            raise ValueError("已达到全局最终重试保护上限")
        if remaining_node_attempts(run, run.current_node) <= 0:
            raise ValueError(f"节点 {run.current_node} 已达到业务重试预算")
        require_all_roles(user, ["on-call-lead"])
        run.status = RunStatus.QUEUED
        run.error_code = None
        run.error_detail = None
        self.audit(
            run,
            user.username,
            "run.retry_queued",
            "失败节点已使用原计划哈希和幂等上下文重新入队。",
            {
                "node": run.current_node,
                "remaining_node_attempts": remaining_node_attempts(
                    run, run.current_node
                ),
            },
        )
        self.store.save_run_and_enqueue(run, "retry")
        return run

    def checkpoint(self, run: RunRecord, lease: LeaseContext) -> RunRecord:
        self.assert_lease(lease)
        return self.store.save_run_with_lease(
            run, lease.job_id, lease.worker_id, lease.fencing_token
        )

    def assert_lease(self, lease: LeaseContext) -> None:
        lease.assert_active()
        self.store.assert_job_lease(
            lease.job_id, lease.worker_id, lease.fencing_token
        )
        lease.assert_active()

    def lease_guard(self, lease: LeaseContext) -> Callable[[], None]:
        return lambda: self.assert_lease(lease)

    def require_run(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"运行不存在：{run_id}")
        return run

    @staticmethod
    def audit(
        run: RunRecord,
        actor: str,
        action: str,
        detail: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        append_audit_event(run, actor, action, detail, metadata)


# Compatibility aliases used by existing tests and integrations while callers move
# to the explicit public methods.
LifecycleMixin._checkpoint = LifecycleMixin.checkpoint
LifecycleMixin._assert_lease = LifecycleMixin.assert_lease
LifecycleMixin._lease_guard = LifecycleMixin.lease_guard
LifecycleMixin._require_run = LifecycleMixin.require_run
LifecycleMixin._audit = LifecycleMixin.audit
LifecycleMixin._run_node = LifecycleMixin.run_node
