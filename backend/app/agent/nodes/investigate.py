from __future__ import annotations

from ..context import EnginePort, LeaseContext, NodeOutcome
from ...registry import SERVICE_REGISTRY
from ...registry.services import build_observation_plan
from ...schemas import Check, PlanStep, RiskLevel, RollbackPlan, RunRecord, RunStatus


def baseline_plan(engine: EnginePort, record: RunRecord) -> list[PlanStep]:
    if not record.incident.experiment_id:
        if (
            record.incident.environment == "staging"
            and engine.kubernetes_connector_enabled
        ):
            return [
                PlanStep(
                    id="step-inspect-kubernetes-workload",
                    title="读取 Kubernetes 工作负载",
                    objective="从隔离 namespace 读取 Deployment、Pod、事件与服务端容量信号。",
                    tool_name="inspect_kubernetes_workload",
                    tool_input={
                        "namespace": engine.kubernetes_staging_namespace,
                        "deployment": record.incident.service,
                        "include_events": True,
                        "event_limit": 10,
                        "wait_for_ready_replicas": None,
                        "timeout_seconds": 0,
                    },
                    evidence_ids=[
                        "incident:input",
                        *[source.chunk_id for source in record.sources[:2]],
                    ],
                    success_criteria=[
                        Check(
                            field="__result_status__",
                            operator="eq",
                            value="succeeded",
                            description="Kubernetes 只读取证必须明确成功",
                        )
                    ],
                    rollback=RollbackPlan(
                        mode="manual",
                        rationale="只读调用没有业务副作用；失败时由值班人员检查 ServiceAccount、RBAC 与 API Server。",
                    ),
                    risk=RiskLevel.LOW,
                    rationale="staging 事件先读取受 RBAC 和白名单约束的真实 Kubernetes 对象，不能使用管理员 kubeconfig。",
                )
            ]
        return [
            PlanStep(
                id="step-observe-prometheus-slo",
                title="读取生产 SLO 指标",
                objective="通过只读 Prometheus HTTP API 建立请求量、错误率、延迟和存活基线。",
                tool_name="query_prometheus_slo",
                tool_input={
                    "service": record.incident.service,
                    "environment": record.incident.environment,
                    "window_minutes": 15,
                },
                evidence_ids=[
                    "incident:input",
                    *[source.chunk_id for source in record.sources[:2]],
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

    evidence = [
        "incident:input",
        *[source.chunk_id for source in record.sources[:2]],
    ]
    service = SERVICE_REGISTRY.get(record.incident.service)
    if service is not None:
        registered = build_observation_plan(service, record.incident, evidence)
        if registered:
            return registered

    target = {
        "experiment_id": record.incident.experiment_id,
        "service": record.incident.service,
    }
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
            tool_input={
                **target,
                "query": "error OR timeout OR unauthorized OR stale",
                "limit": 30,
            },
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


def run(engine: EnginePort, record: RunRecord, lease: LeaseContext) -> NodeOutcome:
    del lease
    has_real_observer = (
        record.incident.environment == "staging"
        and engine.kubernetes_connector_enabled
    ) or (
        record.incident.environment in {"production", "staging"}
        and engine.production_observation_enabled
    )
    if not record.incident.experiment_id and not has_real_observer:
        record.resolution = (
            "事件没有绑定受控实验，也没有配置匹配环境的真实观测源；已转人工接入遥测。"
        )
        return NodeOutcome(
            record.resolution,
            "missing observation provider",
            next_node="handed_off",
            status=RunStatus.HANDED_OFF,
        )
    proposal_plan = baseline_plan(engine, record)
    allowed = {source.chunk_id for source in record.sources} | {"incident:input"}
    compiled, decision = engine.policy_compiler.compile(
        proposal_plan,
        phase="investigation",
        allowed_evidence_ids=allowed,
        observation_ids=set(),
        experiment_id=record.incident.experiment_id,
        service_id=record.incident.service,
    )
    record.investigation_plan = compiled
    record.investigation_policy = decision
    if not decision.accepted:
        record.resolution = "调查计划未通过策略编译，已转人工。"
        return NodeOutcome(
            record.resolution,
            "; ".join(issue.code for issue in decision.issues),
            next_node="handed_off",
            status=RunStatus.HANDED_OFF,
        )
    return NodeOutcome(
        "确定性最小观测集已通过策略编译；LLM 不参与低熵、无副作用的基础取证。",
        decision.plan_hash or "",
        next_node="observe",
    )
