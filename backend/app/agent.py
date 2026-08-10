from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from .model_adapter import ModelAdapter
from .policy import PolicyCompiler
from .retrieval import Retriever
from .schemas import (
    Approval,
    ApprovalVote,
    AuditEvent,
    Check,
    Incident,
    Observation,
    PlanStep,
    RiskLevel,
    RollbackPlan,
    RunRecord,
    RunStatus,
    TraceStatus,
    TraceStep,
    UserIdentity,
    VerificationResult,
    WorkerLeaseEvent,
    utc_now,
)
from .security import AuthorizationError, require_all_roles
from .store import ConcurrencyError, LeaseLostError, Store
from .tools import TOOL_REGISTRY, ToolExecutor


SYSTEM_AGENT = UserIdentity(
    username="agent@harbor.local",
    display_name="Harbor 只读执行身份",
    roles=["observer", "operator"],
)

SERVICE_RUNBOOK_ROUTE = {
    "checkout-api": ["RB-101"],
    "invoice-worker": ["RB-104"],
    "partner-gateway": ["RB-203"],
    "catalog-api": ["RB-310"],
    "recommendation-api": ["RB-429", "OPS-12"],
}


@dataclass(frozen=True)
class LeaseContext:
    job_id: str
    worker_id: str
    fencing_token: int
    recovered: bool = False
    lost_event: threading.Event | None = None

    def assert_active(self) -> None:
        if self.lost_event is not None and self.lost_event.is_set():
            raise LeaseLostError(
                f"job {self.job_id} lease was invalidated during execution"
            )


NODE_LABELS = {
    "intake": "事件接收",
    "retrieve": "知识检索",
    "investigate": "调查计划",
    "observe": "工具观测",
    "diagnose": "诊断与修复计划",
    "policy": "策略编译",
    "gate": "风险门",
    "execute": "受控执行",
    "verify": "结果验证",
    "finalize": "完成归档",
}


class AgentEngine:
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
        self.medium_risk_approval_quorum = medium_risk_approval_quorum
        self.high_risk_approval_quorum = high_risk_approval_quorum
        self.enforce_requester_separation = enforce_requester_separation
        self.run_mode = run_mode
        self.production_observation_enabled = production_observation_enabled

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
            audit=[
                AuditEvent(
                    id=f"AUD-{uuid4().hex[:10].upper()}",
                    actor=actor_name,
                    action="run.created",
                    detail="自由文本事件已保存并进入持久任务队列。",
                    metadata={
                        "has_experiment": bool(incident.experiment_id),
                        "tenant_id": tenant_id,
                    },
                )
            ],
        )
        self.store.save_run_and_enqueue(run, "start")
        return run

    def process(self, run_id: str, lease: LeaseContext) -> RunRecord:
        self._assert_lease(lease)
        run = self._require_run(run_id)
        if run.status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.HANDED_OFF}:
            return run
        if run.cancellation_requested:
            run.status = RunStatus.CANCELLED
            run.current_node = "cancelled"
            return self._checkpoint(run, lease)

        run.status = RunStatus.RUNNING
        run.attempt += 1
        run.lease_history.append(
            WorkerLeaseEvent(
                worker_id=lease.worker_id,
                fencing_token=lease.fencing_token,
                action="recovered" if lease.recovered else "claimed",
            )
        )
        self._audit(
            run,
            lease.worker_id,
            "job.recovered" if lease.recovered else "job.claimed",
            f"worker 使用 fencing token {lease.fencing_token} 处理任务。",
            {"job_id": lease.job_id},
        )
        self._checkpoint(run, lease)

        transitions = 0
        while run.status == RunStatus.RUNNING:
            transitions += 1
            if transitions > 24:
                run.status = RunStatus.FAILED
                run.error_code = "NODE_LIMIT_EXCEEDED"
                run.error_detail = "workflow exceeded the bounded node transition limit"
                self._audit(run, "system", "run.failed", "工作流超过节点上限，已停止。")
                break
            if run.cancellation_requested:
                run.status = RunStatus.CANCELLED
                run.current_node = "cancelled"
                break
            node = run.current_node
            handler = getattr(self, f"_node_{node}", None)
            if handler is None:
                run.status = RunStatus.FAILED
                run.error_code = "UNKNOWN_NODE"
                run.error_detail = f"unknown workflow node: {node}"
                break
            self._run_node(run, node, handler, lease)
            if run.status in {
                RunStatus.AWAITING_APPROVAL,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.HANDED_OFF,
                RunStatus.CANCELLED,
            }:
                break
        return self._checkpoint(run, lease)

    def decide(
        self,
        run_id: str,
        decision: str,
        user: UserIdentity,
        note: str,
        expected_version: int,
    ) -> RunRecord:
        run = self._require_run(run_id)
        if run.tenant_id != user.tenant_id:
            raise AuthorizationError("run does not belong to the authenticated tenant")
        if run.version != expected_version:
            raise ConcurrencyError(
                f"运行 {run.id} 版本冲突：期望 {expected_version}，当前 {run.version}"
            )
        if run.status != RunStatus.AWAITING_APPROVAL:
            raise ValueError("当前运行不在等待审批状态")
        if not run.policy.accepted or not run.policy.plan_hash:
            raise ValueError("计划没有通过策略编译，不能审批")
        if run.approval.plan_hash != run.policy.plan_hash:
            raise ValueError("审批绑定的计划哈希已失效")
        require_all_roles(user, run.approval.required_roles)
        if (
            run.approval.separation_of_duties
            and run.approval.requester
            and user.username.lower() == run.approval.requester.lower()
        ):
            raise AuthorizationError("requester cannot approve or deny the same change")
        if any(
            vote.approver.lower() == user.username.lower()
            for vote in run.approval.votes
        ):
            raise ValueError("the same subject cannot vote twice")
        vote = ApprovalVote(
            approver=user.username,
            display_name=user.display_name,
            roles=user.roles,
            tenant_id=user.tenant_id,
            plan_hash=run.policy.plan_hash,
            decision=decision,
            note=note,
        )
        votes = [*run.approval.votes, vote]
        if decision == "approve":
            if len(votes) < run.approval.required_approvals:
                run.approval = run.approval.model_copy(
                    update={
                        "decision": "pending",
                        "votes": votes,
                    }
                )
                self._audit(
                    run,
                    user.username,
                    "approval.vote_recorded",
                    (
                        f"已记录第 {len(votes)} 票；还需要 "
                        f"{run.approval.required_approvals - len(votes)} 个不同主体。"
                    ),
                    {
                        "plan_hash": run.policy.plan_hash,
                        "votes": len(votes),
                        "required_approvals": run.approval.required_approvals,
                    },
                )
                return self.store.save_run(run)
            run.approval = Approval(
                required=True,
                decision="approved",
                plan_hash=run.policy.plan_hash,
                required_roles=run.approval.required_roles,
                required_approvals=run.approval.required_approvals,
                separation_of_duties=run.approval.separation_of_duties,
                requester=run.approval.requester,
                votes=votes,
                decided_by=user.username,
                decided_roles=user.roles,
                decided_at=utc_now(),
                note=note,
            )
            run.status = RunStatus.QUEUED
            run.current_node = "execute"
            self._audit(
                run,
                user.username,
                "approval.approved",
                "独立审批人数达到 quorum；不可变计划已重新入队。",
                {
                    "plan_hash": run.policy.plan_hash,
                    "roles": user.roles,
                    "approvers": [item.approver for item in votes],
                    "required_approvals": run.approval.required_approvals,
                },
            )
            self.store.save_run_and_enqueue(run, "resume")
            return run
        if decision != "deny":
            raise ValueError("decision must be approve or deny")
        run.approval = Approval(
            required=True,
            decision="denied",
            plan_hash=run.policy.plan_hash,
            required_roles=run.approval.required_roles,
            required_approvals=run.approval.required_approvals,
            separation_of_duties=run.approval.separation_of_duties,
            requester=run.approval.requester,
            votes=votes,
            decided_by=user.username,
            decided_roles=user.roles,
            decided_at=utc_now(),
            note=note,
        )
        run.status = RunStatus.HANDED_OFF
        run.current_node = "handed_off"
        run.resolution = "审批拒绝；没有执行任何待批准写动作，已转人工。"
        self._audit(run, user.username, "approval.denied", run.resolution)
        return self.store.save_run(run)

    def cancel(self, run_id: str, user: UserIdentity, expected_version: int) -> RunRecord:
        run = self._require_run(run_id)
        if run.tenant_id != user.tenant_id:
            raise AuthorizationError("run does not belong to the authenticated tenant")
        if run.version != expected_version:
            raise ConcurrencyError(
                f"运行 {run.id} 版本冲突：期望 {expected_version}，当前 {run.version}"
            )
        if run.status in {RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.HANDED_OFF}:
            raise ValueError("终态运行不能取消")
        run.cancellation_requested = True
        if run.status in {RunStatus.QUEUED, RunStatus.AWAITING_APPROVAL, RunStatus.FAILED}:
            run.status = RunStatus.CANCELLED
            run.current_node = "cancelled"
        self._audit(run, user.username, "run.cancel_requested", "用户请求取消运行。")
        return self.store.save_run(run)

    def retry_failed(self, run_id: str, user: UserIdentity, expected_version: int) -> RunRecord:
        run = self._require_run(run_id)
        if run.tenant_id != user.tenant_id:
            raise AuthorizationError("run does not belong to the authenticated tenant")
        if run.version != expected_version:
            raise ConcurrencyError(
                f"运行 {run.id} 版本冲突：期望 {expected_version}，当前 {run.version}"
            )
        if run.status != RunStatus.FAILED:
            raise ValueError("只有失败运行可以重试")
        if run.attempt >= run.max_attempts:
            raise ValueError("已达到最大重试次数")
        require_all_roles(user, ["on-call-lead"])
        run.status = RunStatus.QUEUED
        run.error_code = None
        run.error_detail = None
        # Re-enter the failed node. Tool idempotency prevents repeated side effects.
        self._audit(run, user.username, "run.retry_queued", "失败节点已使用原幂等上下文重新入队。")
        self.store.save_run_and_enqueue(run, "retry")
        return run

    def _run_node(self, run: RunRecord, node: str, handler, lease: LeaseContext) -> None:
        self._assert_lease(lease)
        started = time.perf_counter()
        trace = TraceStep(
            id=f"TR-{uuid4().hex[:10].upper()}",
            node=node,
            label=NODE_LABELS.get(node, node),
            status=TraceStatus.RUNNING,
            summary="节点开始执行。",
        )
        try:
            summary, output = handler(run, lease)
            self._assert_lease(lease)
            trace.status = (
                TraceStatus.WAITING
                if run.status == RunStatus.AWAITING_APPROVAL
                else TraceStatus.BLOCKED
                if run.status == RunStatus.HANDED_OFF
                else TraceStatus.COMPLETED
            )
            trace.summary = summary
            trace.output_preview = output[:500]
        except LeaseLostError:
            # Ownership has moved to a newer fencing token. Do not persist this
            # worker's in-memory trace or turn a lease loss into a business failure.
            raise
        except Exception as exc:
            trace.status = TraceStatus.FAILED
            trace.summary = f"{type(exc).__name__}: {str(exc)[:240]}"
            run.status = RunStatus.FAILED
            run.error_code = type(exc).__name__.upper()
            run.error_detail = str(exc)[:1000]
            self._audit(run, "system", "node.failed", trace.summary, {"node": node})
        trace.duration_ms = max(1, round((time.perf_counter() - started) * 1000))
        run.traces.append(trace)
        self._checkpoint(run, lease)

    def _node_intake(self, run: RunRecord, lease: LeaseContext) -> tuple[str, str]:
        del lease
        run.current_node = "retrieve"
        return "自由事件已规范化；运行时对象不包含预期根因、预期计划或预期结果。", run.incident.summary

    def _node_retrieve(self, run: RunRecord, lease: LeaseContext) -> tuple[str, str]:
        del lease
        # The long narrative is untrusted and may contain prompt-injection terms that
        # poison retrieval. Route with bounded, typed fields; still pass the narrative
        # to the model later as explicitly untrusted incident data.
        query = " ".join(
            [run.incident.service, run.incident.title, *run.incident.symptoms]
        )
        run.sources = self.retriever.search(
            query,
            limit=6,
            preferred_doc_ids=SERVICE_RUNBOOK_ROUTE.get(run.incident.service, []),
        )
        run.current_node = "investigate"
        return f"检索到 {len(run.sources)} 个去重文档片段。", ", ".join(item.chunk_id for item in run.sources)

    def _node_investigate(self, run: RunRecord, lease: LeaseContext) -> tuple[str, str]:
        del lease
        if (
            not run.incident.experiment_id
            and (
                run.incident.environment not in {"production", "staging"}
                or not self.production_observation_enabled
            )
        ):
            run.status = RunStatus.HANDED_OFF
            run.current_node = "handed_off"
            run.resolution = "事件没有绑定受控实验，也没有配置生产只读观测源；已转人工接入真实遥测。"
            return run.resolution, "missing observation provider"
        proposal_plan = self._baseline_investigation_plan(run)
        allowed = {source.chunk_id for source in run.sources} | {"incident:input"}
        compiled, decision = self.policy_compiler.compile(
            proposal_plan,
            phase="investigation",
            allowed_evidence_ids=allowed,
            observation_ids=set(),
            experiment_id=run.incident.experiment_id,
        )
        run.investigation_plan = compiled
        run.investigation_policy = decision
        if not decision.accepted:
            run.status = RunStatus.HANDED_OFF
            run.current_node = "handed_off"
            run.resolution = "调查计划未通过策略编译，已转人工。"
            return run.resolution, "; ".join(issue.code for issue in decision.issues)
        run.current_node = "observe"
        return "确定性最小观测集已通过策略编译；LLM 不参与低熵、无副作用的基础取证。", decision.plan_hash or ""

    @staticmethod
    def _baseline_investigation_plan(run: RunRecord) -> list[PlanStep]:
        """Always collect the minimum safe telemetry before asking an LLM to diagnose."""

        if not run.incident.experiment_id:
            return [
                PlanStep(
                    id="step-observe-prometheus-slo",
                    title="读取生产 SLO 指标",
                    objective="通过只读 Prometheus HTTP API 建立请求量、错误率、延迟和存活基线。",
                    tool_name="query_prometheus_slo",
                    tool_input={
                        "service": run.incident.service,
                        "environment": run.incident.environment,
                        "window_minutes": 15,
                    },
                    evidence_ids=[
                        "incident:input",
                        *[source.chunk_id for source in run.sources[:2]],
                    ],
                    success_criteria=[
                        Check(
                            field="__result_status__",
                            operator="eq",
                            value="succeeded",
                            description="Prometheus 查询通道必须返回明确成功状态",
                        )
                    ],
                    rollback=RollbackPlan(
                        mode="manual",
                        rationale="只读查询没有业务副作用；失败时由值班人员检查 Prometheus 标签和访问策略。",
                    ),
                    risk=RiskLevel.LOW,
                    rationale="生产事件先以服务端固定 PromQL 模板读取真实指标，不接受模型生成任意查询。",
                )
            ]

        target = {
            "experiment_id": run.incident.experiment_id,
            "service": run.incident.service,
        }
        evidence = ["incident:input", *[source.chunk_id for source in run.sources[:2]]]
        success = [
            Check(
                field="__result_status__",
                operator="eq",
                value="succeeded",
                description="基础观测通道必须返回明确成功状态",
            )
        ]
        rollback = RollbackPlan(
            mode="manual", rationale="只读调查不产生业务副作用；失败时停止并检查观测连接。"
        )
        return [
            PlanStep(
                id="step-observe-metrics",
                title="采集核心时序指标",
                objective="建立错误、延迟、队列或一致性指标的执行前基线。",
                tool_name="query_metrics",
                tool_input={**target, "window_minutes": 15},
                evidence_ids=evidence,
                success_criteria=success,
                rollback=rollback,
                risk=RiskLevel.LOW,
                rationale="指标基线用于根因判断、前置条件和修复后的独立比较。",
            ),
            PlanStep(
                id="step-observe-logs",
                title="采集脱敏错误日志",
                objective="从受控日志样本中识别具体异常类型且不暴露 secret。",
                tool_name="inspect_logs",
                tool_input={**target, "query": "error OR timeout OR unauthorized OR stale", "limit": 30},
                evidence_ids=evidence,
                success_criteria=success,
                rollback=rollback,
                risk=RiskLevel.LOW,
                rationale="指标只能描述现象，日志用于区分容量、凭据、依赖和一致性故障。",
            ),
            PlanStep(
                id="step-observe-status",
                title="采集实例与副本状态",
                objective="获得实例健康和当前容量，避免对未知目标执行写动作。",
                tool_name="get_service_status",
                tool_input={**target, "include_instances": True},
                evidence_ids=evidence,
                success_criteria=success,
                rollback=rollback,
                risk=RiskLevel.LOW,
                rationale="具体实例和副本数必须来自工具观测，不能由模型猜测。",
            ),
        ]

    def _node_observe(self, run: RunRecord, lease: LeaseContext) -> tuple[str, str]:
        plan_hash_value = run.investigation_policy.plan_hash
        if not plan_hash_value:
            raise ValueError("investigation plan hash is missing")
        for step in self._topological(run.investigation_plan):
            if any(observation.step_id == step.id for observation in run.observations):
                continue
            result = self.tool_executor.execute(
                step,
                run_id=run.id,
                plan_hash_value=plan_hash_value,
                actor=SYSTEM_AGENT,
                attempt=run.attempt,
                previous_results=run.tool_results,
                job_id=lease.job_id,
                fencing_token=lease.fencing_token,
                lease_guard=self._lease_guard(lease),
            )
            run.tool_results.append(result)
            if result.status not in {"succeeded", "skipped"}:
                run.status = RunStatus.HANDED_OFF
                run.current_node = "handed_off"
                run.resolution = f"只读观测失败或结果未知：{result.summary}"
                self._checkpoint(run, lease)
                return run.resolution, result.error or result.summary
            observation = Observation(
                id=f"OBS-{uuid4().hex[:10].upper()}",
                step_id=step.id,
                tool_name=step.tool_name,
                summary=result.summary,
                data=result.output,
                transport=result.transport,
                source_uri=result.output.get("source_uri"),
            )
            run.observations.append(observation)
            self._checkpoint(run, lease)
            if (
                step.tool_name == "query_prometheus_slo"
                and result.output.get("sample_count") == 0
            ):
                run.status = RunStatus.HANDED_OFF
                run.current_node = "handed_off"
                run.resolution = (
                    "Prometheus 查询成功，但目标服务标签没有返回可归因指标；"
                    "未调用模型、未执行写动作，已转人工核对指标标签与采集状态。"
                )
                self._checkpoint(run, lease)
                return run.resolution, "empty Prometheus observation"
        run.current_node = "diagnose"
        return f"收集到 {len(run.observations)} 条独立工具观测。", ", ".join(item.id for item in run.observations)

    def _node_diagnose(self, run: RunRecord, lease: LeaseContext) -> tuple[str, str]:
        planning_sources = [
            source
            for source in run.sources
            if source.retrieval_channel in {"routed", "pinned"}
        ]
        self._assert_lease(lease)
        proposal, invocation = self.model_adapter.propose(
            phase="remediation",
            incident=run.incident,
            sources=planning_sources,
            observations=run.observations,
        )
        self._assert_lease(lease)
        run.model_calls.append(invocation)
        run.model_proposal = proposal
        run.diagnosis = proposal.diagnosis
        run.confidence = proposal.confidence
        run.plan = proposal.plan
        if invocation.status == "fallback":
            run.run_mode = "model-degraded"
        if not run.incident.experiment_id:
            run.plan = []
            run.status = RunStatus.HANDED_OFF
            run.current_node = "handed_off"
            run.resolution = (
                "已完成生产 Prometheus 只读取证和模型诊断；当前未配置生产写连接器，"
                "因此不会把实验室动作映射到真实系统，已携带证据转人工。"
            )
            return run.resolution, proposal.diagnosis
        if proposal.needs_handoff:
            run.status = RunStatus.HANDED_OFF
            run.current_node = "handed_off"
            run.resolution = f"没有证据支持白名单内的安全修复：{proposal.handoff_reason or proposal.diagnosis}"
            return run.resolution, proposal.diagnosis
        run.current_node = "policy"
        return "模型基于工具观测生成了动态修复计划。", proposal.diagnosis

    def _node_policy(self, run: RunRecord, lease: LeaseContext) -> tuple[str, str]:
        del lease
        allowed = (
            {source.chunk_id for source in run.sources}
            | {observation.id for observation in run.observations}
            | {"incident:input"}
        )
        observation_ids = {observation.id for observation in run.observations}
        compiled, decision = self.policy_compiler.compile(
            run.plan,
            phase="remediation",
            allowed_evidence_ids=allowed,
            observation_ids=observation_ids,
            experiment_id=run.incident.experiment_id,
            observation_context=self._observation_context(run),
        )
        run.plan = compiled
        run.policy = decision
        run.risk_level = decision.effective_risk
        if not decision.accepted:
            run.status = RunStatus.HANDED_OFF
            run.current_node = "handed_off"
            run.resolution = "动态计划未通过策略编译，没有执行写动作。"
            self._audit(
                run,
                "policy-compiler",
                "plan.rejected",
                run.resolution,
                {"issues": [issue.model_dump(mode="json") for issue in decision.issues]},
            )
            return run.resolution, "; ".join(issue.code for issue in decision.issues)
        self._audit(
            run,
            "policy-compiler",
            "plan.accepted",
            "动态计划通过确定性策略编译。",
            {"plan_hash": decision.plan_hash, "required_roles": decision.required_roles},
        )
        run.current_node = "gate"
        return "工具、参数、证据、依赖、风险和回滚检查通过。", decision.plan_hash or ""

    def _node_gate(self, run: RunRecord, lease: LeaseContext) -> tuple[str, str]:
        del lease
        if not run.policy.plan_hash:
            raise ValueError("compiled plan hash is missing")
        requires_approval = run.risk_level in {RiskLevel.MEDIUM, RiskLevel.HIGH, "medium", "high"}
        if requires_approval:
            required_approvals = (
                self.high_risk_approval_quorum
                if run.risk_level in {RiskLevel.HIGH, "high"}
                else self.medium_risk_approval_quorum
            )
            run.approval = Approval(
                required=True,
                decision="pending",
                plan_hash=run.policy.plan_hash,
                required_roles=run.policy.required_roles,
                required_approvals=required_approvals,
                separation_of_duties=self.enforce_requester_separation,
                requester=run.created_by,
            )
            run.status = RunStatus.AWAITING_APPROVAL
            run.current_node = "approval"
            self._audit(
                run,
                "risk-gate",
                "approval.requested",
                "中高风险动作已暂停，等待不同主体满足审批 quorum。",
                {
                    "plan_hash": run.policy.plan_hash,
                    "roles": run.policy.required_roles,
                    "required_approvals": required_approvals,
                    "requester": run.created_by,
                },
            )
            return "风险门暂停了执行；尚未产生写副作用。", json.dumps(run.policy.required_roles, ensure_ascii=False)
        run.approval = Approval(
            required=False,
            decision="not_required",
            plan_hash=run.policy.plan_hash,
            required_roles=run.policy.required_roles,
            required_approvals=0,
            separation_of_duties=False,
            requester=run.created_by,
        )
        run.current_node = "execute"
        return "低风险计划可由受限系统身份执行。", run.policy.plan_hash

    def _node_execute(self, run: RunRecord, lease: LeaseContext) -> tuple[str, str]:
        if not run.policy.accepted or not run.policy.plan_hash:
            raise ValueError("uncompiled plan cannot execute")
        actor = self._execution_actor(run)

        completed_step_ids = {
            result.step_id for result in run.tool_results if result.status in {"succeeded", "skipped"}
        }
        for step in self._topological(run.plan):
            if step.id in completed_step_ids:
                continue
            if not set(step.depends_on).issubset(completed_step_ids):
                raise ValueError(f"dependencies have not succeeded for {step.id}")
            precondition_context = self._observation_context(run)
            failed_preconditions = [
                check.description
                for check in step.preconditions
                if not self._check(check, precondition_context, "succeeded")[0]
            ]
            if failed_preconditions:
                run.status = RunStatus.HANDED_OFF
                run.current_node = "handed_off"
                run.resolution = f"执行前置条件已变化：{'; '.join(failed_preconditions)}"
                return run.resolution, "precondition_changed"
            result = self.tool_executor.execute(
                step,
                run_id=run.id,
                plan_hash_value=run.policy.plan_hash,
                actor=actor,
                attempt=run.attempt,
                previous_results=run.tool_results,
                job_id=lease.job_id,
                fencing_token=lease.fencing_token,
                lease_guard=self._lease_guard(lease),
            )
            run.tool_results.append(result)
            self._audit(
                run,
                actor.username,
                "tool.executed",
                result.summary,
                {
                    "tool": step.tool_name,
                    "step_id": step.id,
                    "status": result.status,
                    "capability_jti": result.capability_jti,
                    "idempotency_key": result.idempotency_key,
                },
            )
            self._checkpoint(run, lease)
            if result.status == "unknown":
                run.status = RunStatus.HANDED_OFF
                run.current_node = "handed_off"
                run.resolution = "写操作结果未知；禁止换幂等键继续，已转人工核对。"
                return run.resolution, result.error or "unknown"
            if result.status == "failed":
                run.status = RunStatus.FAILED
                run.error_code = "TOOL_EXECUTION_FAILED"
                run.error_detail = result.error or result.summary
                return "工具明确失败，工作流停止。", run.error_detail
            completed_step_ids.add(step.id)
        run.current_node = "verify"
        return "所有计划动作已执行或被持久幂等记录确认。", f"{len(run.plan)} steps"

    @staticmethod
    def _execution_actor(run: RunRecord) -> UserIdentity:
        """Reconstruct the approved principal for actions and compensating actions."""
        if not run.approval.required:
            return SYSTEM_AGENT
        if run.approval.decision != "approved" or run.approval.plan_hash != run.policy.plan_hash:
            raise AuthorizationError("plan is not approved or approval hash is stale")
        if len(run.approval.votes) < run.approval.required_approvals:
            raise AuthorizationError("approval quorum is incomplete")
        if any(vote.plan_hash != run.policy.plan_hash for vote in run.approval.votes):
            raise AuthorizationError("approval vote is bound to a stale plan hash")
        actor = UserIdentity(
            username=run.approval.decided_by or "unknown",
            display_name=run.approval.decided_by or "unknown",
            roles=run.approval.decided_roles,
            tenant_id=run.tenant_id,
        )
        require_all_roles(actor, run.policy.required_roles)
        return actor

    def _execute_automatic_rollback(
        self,
        run: RunRecord,
        step: PlanStep,
        lease: LeaseContext,
    ) -> tuple[bool, str]:
        """Execute a hash-bound compensation and independently confirm restored state."""
        if (
            step.rollback.mode != "tool"
            or not step.rollback.tool_name
            or not run.policy.plan_hash
        ):
            return False, "manual rollback required"
        acted = any(
            result.step_id == step.id and result.status in {"succeeded", "skipped"}
            for result in run.tool_results
        )
        if not acted:
            return True, "action did not commit; rollback not needed"
        spec = TOOL_REGISTRY[step.rollback.tool_name]
        suffix = step.id.removeprefix("step-")
        rollback_step = PlanStep(
            id=f"step-rollback-{suffix}",
            title="执行补偿回滚",
            objective="验证失败后恢复策略编译时记录的执行前状态。",
            tool_name=step.rollback.tool_name,
            tool_input=step.rollback.tool_input,
            evidence_ids=step.evidence_ids,
            success_criteria=[
                Check(
                    field="__result_status__",
                    operator="eq",
                    value="succeeded",
                    description="补偿工具必须返回明确成功状态",
                )
            ],
            rollback=RollbackPlan(
                mode="manual",
                rationale="补偿动作失败或结果未知时禁止递归自动回滚，立即由值班负责人接管。",
            ),
            risk=spec.risk,
            rationale="该补偿动作已包含在原始不可变计划哈希与人工审批范围内。",
        )
        actor = self._execution_actor(run)
        rollback_result = self.tool_executor.execute(
            rollback_step,
            run_id=run.id,
            plan_hash_value=run.policy.plan_hash,
            actor=actor,
            attempt=run.attempt,
            previous_results=run.tool_results,
            job_id=lease.job_id,
            fencing_token=lease.fencing_token,
            lease_guard=self._lease_guard(lease),
        )
        run.tool_results.append(rollback_result)
        self._audit(
            run,
            actor.username,
            "rollback.executed",
            rollback_result.summary,
            {
                "original_step_id": step.id,
                "rollback_step_id": rollback_step.id,
                "tool": rollback_step.tool_name,
                "status": rollback_result.status,
                "capability_jti": rollback_result.capability_jti,
                "idempotency_key": rollback_result.idempotency_key,
            },
        )
        self._checkpoint(run, lease)
        if rollback_result.status not in {"succeeded", "skipped"}:
            return False, rollback_result.error or rollback_result.summary

        # The current automatic allowlist contains scale_workers. Its compensation is
        # only considered successful after an independent service-status read confirms
        # the pre-change replica count.
        target_replicas = int(step.rollback.tool_input["target_replicas"])
        verify_step = PlanStep(
            id=f"step-verify-rollback-{suffix}",
            title="复查回滚状态",
            objective="通过独立只读通道确认副本数已恢复到执行前状态。",
            tool_name="get_service_status",
            tool_input={
                "experiment_id": run.incident.experiment_id,
                "service": run.incident.service,
                "include_instances": True,
            },
            evidence_ids=step.evidence_ids,
            success_criteria=[
                Check(
                    field="replicas",
                    operator="eq",
                    value=target_replicas,
                    description="副本数恢复到执行前观测值",
                )
            ],
            rollback=RollbackPlan(
                mode="manual",
                rationale="只读复查没有副作用；失败时由值班负责人直接检查编排平台。",
            ),
            risk=RiskLevel.LOW,
            rationale="不能把补偿工具的成功响应直接当成状态已经恢复。",
        )
        verify_result = self.tool_executor.execute(
            verify_step,
            run_id=run.id,
            plan_hash_value=run.policy.plan_hash,
            actor=SYSTEM_AGENT,
            attempt=run.attempt,
            previous_results=run.tool_results,
            job_id=lease.job_id,
            fencing_token=lease.fencing_token,
            lease_guard=self._lease_guard(lease),
        )
        run.tool_results.append(verify_result)
        actual = verify_result.output.get("replicas")
        passed = (
            verify_result.status in {"succeeded", "skipped"}
            and actual == target_replicas
        )
        run.verification.append(
            VerificationResult(
                step_id=rollback_step.id,
                passed=passed,
                checks=[
                    {
                        "field": "replicas",
                        "operator": "eq",
                        "expected": target_replicas,
                        "actual": actual,
                        "passed": passed,
                        "description": "副本数恢复到执行前观测值",
                    }
                ],
                summary="补偿动作已由独立状态读取确认。"
                if passed
                else "补偿动作没有通过独立状态确认。",
            )
        )
        self._audit(
            run,
            "system",
            "rollback.verified" if passed else "rollback.verification_failed",
            f"rollback target replicas={target_replicas}, actual={actual}",
            {"original_step_id": step.id, "verification_step_id": verify_step.id},
        )
        self._checkpoint(run, lease)
        return passed, "rollback independently verified" if passed else "rollback verification failed"

    def _node_verify(self, run: RunRecord, lease: LeaseContext) -> tuple[str, str]:
        if not run.policy.plan_hash:
            raise ValueError("plan hash missing during verification")
        all_passed = True
        for step in run.plan:
            action_result = next(
                (
                    result
                    for result in reversed(run.tool_results)
                    if result.step_id == step.id and result.status in {"succeeded", "skipped"}
                ),
                None,
            )
            if action_result is None:
                run.verification.append(
                    VerificationResult(step_id=step.id, passed=False, summary="找不到成功的动作结果。")
                )
                all_passed = False
                continue
            verify_step = PlanStep(
                id=f"step-verify-{step.id.removeprefix('step-')}",
                title="复查原始指标",
                objective="重新读取原始指标，避免把工具返回的成功文本当成修复成功。",
                tool_name="query_metrics",
                tool_input={
                    "experiment_id": run.incident.experiment_id,
                    "service": run.incident.service,
                    "window_minutes": 5,
                },
                evidence_ids=step.evidence_ids,
                success_criteria=[
                    Check(field="__result_status__", operator="eq", value="succeeded", description="验证指标读取成功")
                ],
                rollback=RollbackPlan(mode="manual", rationale="验证失败时停止自动动作并人工复核。"),
                risk=RiskLevel.LOW,
                rationale="修复是否成功必须回到原始观测通道确认。",
            )
            verify_result = self.tool_executor.execute(
                verify_step,
                run_id=run.id,
                plan_hash_value=run.policy.plan_hash,
                actor=SYSTEM_AGENT,
                attempt=run.attempt,
                previous_results=run.tool_results,
                job_id=lease.job_id,
                fencing_token=lease.fencing_token,
                lease_guard=self._lease_guard(lease),
            )
            run.tool_results.append(verify_result)
            context = {**action_result.output, **verify_result.output}
            checks: list[dict[str, Any]] = []
            passed = verify_result.status in {"succeeded", "skipped"}
            for check in step.success_criteria:
                check_passed, actual = self._check(check, context, verify_result.status)
                passed = passed and check_passed
                checks.append(
                    {
                        "field": check.field,
                        "operator": check.operator,
                        "expected": check.value,
                        "actual": actual,
                        "passed": check_passed,
                        "description": check.description,
                    }
                )
            run.verification.append(
                VerificationResult(
                    step_id=step.id,
                    passed=passed,
                    checks=checks,
                    summary="成功条件全部满足。" if passed else "至少一个成功条件不满足。",
                )
            )
            all_passed = all_passed and passed
            self._checkpoint(run, lease)
        if not all_passed:
            run.status = RunStatus.FAILED
            run.current_node = "verify"
            automatic_outcomes: list[bool] = []
            manual_required = False
            # Compensate in reverse dependency order. The rollback contract was
            # validated and included in the approved plan hash before execution.
            for step in reversed(self._topological(run.plan)):
                if step.rollback.mode == "tool":
                    passed, _ = self._execute_automatic_rollback(run, step, lease)
                    automatic_outcomes.append(passed)
                else:
                    manual_required = True
                    self._audit(
                        run,
                        "system",
                        "rollback.required",
                        step.rollback.rationale,
                        {"step_id": step.id, "mode": "manual"},
                    )
            if automatic_outcomes and all(automatic_outcomes) and not manual_required:
                run.error_code = "VERIFICATION_FAILED_ROLLED_BACK"
                run.error_detail = (
                    "主动作没有满足成功条件；所有补偿动作已执行并由独立只读通道确认。"
                )
            elif automatic_outcomes and not all(automatic_outcomes):
                run.error_code = "ROLLBACK_FAILED"
                run.error_detail = "主动作验证失败，且至少一个自动补偿动作未能确认恢复。"
            else:
                run.error_code = "VERIFICATION_FAILED_MANUAL_ROLLBACK_REQUIRED"
                run.error_detail = "主动作没有满足成功条件；计划要求人工执行或复核回滚。"
            return run.error_detail, "verification=false"
        run.current_node = "finalize"
        return "所有写动作均由独立指标读取验证。", "verification=true"

    def _node_finalize(self, run: RunRecord, lease: LeaseContext) -> tuple[str, str]:
        del lease
        actions = [
            result.summary
            for result in run.tool_results
            if result.step_id in {step.id for step in run.plan} and result.status in {"succeeded", "skipped"}
        ]
        run.status = RunStatus.COMPLETED
        run.current_node = "completed"
        run.resolution = "；".join(actions) or "验证完成，没有执行写动作。"
        self._audit(run, "system", "run.completed", "隐藏真值未参与运行；成功来自工具状态和条件验证。")
        return "运行完成并归档。", run.resolution

    def _checkpoint(self, run: RunRecord, lease: LeaseContext) -> RunRecord:
        self._assert_lease(lease)
        return self.store.save_run_with_lease(
            run, lease.job_id, lease.worker_id, lease.fencing_token
        )

    def _assert_lease(self, lease: LeaseContext) -> None:
        lease.assert_active()
        self.store.assert_job_lease(
            lease.job_id, lease.worker_id, lease.fencing_token
        )
        lease.assert_active()

    def _lease_guard(self, lease: LeaseContext) -> Callable[[], None]:
        return lambda: self._assert_lease(lease)

    def _require_run(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"运行不存在：{run_id}")
        return run

    @staticmethod
    def _audit(
        run: RunRecord,
        actor: str,
        action: str,
        detail: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        run.audit.append(
            AuditEvent(
                id=f"AUD-{uuid4().hex[:10].upper()}",
                actor=actor,
                action=action,
                detail=detail,
                metadata=metadata or {},
            )
        )

    @staticmethod
    def _topological(plan: list[PlanStep]) -> list[PlanStep]:
        by_id = {step.id: step for step in plan}
        pending = {step.id: set(step.depends_on) for step in plan}
        ordered: list[PlanStep] = []
        while pending:
            ready = sorted(step_id for step_id, dependencies in pending.items() if not dependencies)
            if not ready:
                raise ValueError("plan contains a dependency cycle")
            for step_id in ready:
                ordered.append(by_id[step_id])
                pending.pop(step_id)
                for dependencies in pending.values():
                    dependencies.discard(step_id)
        return ordered

    @staticmethod
    def _observation_context(run: RunRecord) -> dict[str, Any]:
        context: dict[str, Any] = {}
        for observation in run.observations:
            context.update(observation.data)
        return context

    @staticmethod
    def _field(context: dict[str, Any], path: str) -> Any:
        value: Any = context
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                return None
            value = value[part]
        return value

    @classmethod
    def _check(cls, check: Check, context: dict[str, Any], result_status: str) -> tuple[bool, Any]:
        actual = result_status if check.field == "__result_status__" else cls._field(context, check.field)
        expected = check.value
        try:
            if check.operator == "eq":
                return actual == expected, actual
            if check.operator == "ne":
                return actual != expected, actual
            if check.operator == "lt":
                return actual < expected, actual
            if check.operator == "lte":
                return actual <= expected, actual
            if check.operator == "gt":
                return actual > expected, actual
            if check.operator == "gte":
                return actual >= expected, actual
            if check.operator == "contains":
                return expected in actual, actual
            if check.operator == "in":
                return actual in expected, actual
        except (TypeError, ValueError):
            return False, actual
        return False, actual
