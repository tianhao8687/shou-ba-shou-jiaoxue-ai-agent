from __future__ import annotations

from collections.abc import Iterator, Mapping

from ..schemas import (
    Check,
    Incident,
    ModelProposal,
    Observation,
    PlanStep,
    RiskLevel,
    RollbackPlan,
    SourceHit,
)
from .contracts import (
    ObservationDefinition,
    ServiceDefinition,
    observation_values,
    render_template,
)


class ServiceRegistry(Mapping[str, ServiceDefinition]):
    def __init__(self) -> None:
        self._items: dict[str, ServiceDefinition] = {}

    def register(self, definition: ServiceDefinition, *, replace: bool = False) -> None:
        if definition.service_id in self._items and not replace:
            raise ValueError(f"service already registered: {definition.service_id}")
        self._items[definition.service_id] = definition

    def __getitem__(self, service_id: str) -> ServiceDefinition:
        return self._items[service_id]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


SERVICE_REGISTRY = ServiceRegistry()


def _result_succeeded(description: str) -> tuple[Check, ...]:
    return (
        Check(
            field="__result_status__",
            operator="eq",
            value="succeeded",
            description=description,
        ),
    )


COMMON_LAB_OBSERVATIONS = (
    ObservationDefinition(
        id="observe-metrics",
        title="采集核心时序指标",
        objective="建立错误、延迟、队列或一致性指标的执行前基线。",
        tool_name="query_metrics",
        input_template={
            "experiment_id": "$incident.experiment_id",
            "service": "$incident.service",
            "window_minutes": 15,
        },
        success_criteria=_result_succeeded("基础指标观测必须明确成功"),
        rationale="指标基线用于根因判断、前置条件和修复后的独立比较。",
    ),
    ObservationDefinition(
        id="observe-logs",
        title="采集脱敏错误日志",
        objective="从受控日志样本中识别具体异常类型且不暴露 secret。",
        tool_name="inspect_logs",
        input_template={
            "experiment_id": "$incident.experiment_id",
            "service": "$incident.service",
            "query": "error OR timeout OR unauthorized OR stale",
            "limit": 30,
        },
        success_criteria=_result_succeeded("脱敏日志观测必须明确成功"),
        rationale="日志用于区分容量、凭据、依赖和一致性故障。",
    ),
    ObservationDefinition(
        id="observe-status",
        title="采集实例与副本状态",
        objective="获得实例健康和当前容量，避免对未知目标执行写动作。",
        tool_name="get_service_status",
        input_template={
            "experiment_id": "$incident.experiment_id",
            "service": "$incident.service",
            "include_instances": True,
        },
        success_criteria=_result_succeeded("实例状态观测必须明确成功"),
        rationale="具体实例和副本数必须来自工具观测，不能由模型猜测。",
    ),
)

DEFAULT_METRICS_VERIFICATION = ObservationDefinition(
    id="verify-metrics",
    title="复查原始指标",
    objective="重新读取原始指标，不能把写工具响应当作恢复证据。",
    tool_name="query_metrics",
    input_template={
        "experiment_id": "$incident.experiment_id",
        "service": "$incident.service",
        "window_minutes": 5,
    },
    success_criteria=_result_succeeded("验证指标读取必须成功"),
    rationale="独立读取事故原始观测信号。",
)


def _register_builtin(
    service_id: str,
    display_name: str,
    runbooks: tuple[str, ...],
    write_tools: tuple[str, ...],
) -> None:
    observations = tuple(item.tool_name for item in COMMON_LAB_OBSERVATIONS)
    SERVICE_REGISTRY.register(
        ServiceDefinition(
            service_id=service_id,
            display_name=display_name,
            runbook_ids=runbooks,
            allowed_observations=observations,
            allowed_tools=observations + write_tools,
            environment=frozenset({"lab"}),
            observation_steps=COMMON_LAB_OBSERVATIONS,
            verification_observation=DEFAULT_METRICS_VERIFICATION,
            metadata={"source": "built-in"},
        )
    )


_register_builtin("checkout-api", "结算接口", ("RB-101",), ("restart_service",))
_register_builtin("invoice-worker", "开票消费者", ("RB-104",), ("scale_workers",))
_register_builtin("partner-gateway", "合作方网关", ("RB-203",), ("rotate_credential",))
_register_builtin("catalog-api", "商品目录接口", ("RB-310",), ("refresh_cache",))
_register_builtin("recommendation-api", "推荐接口", ("RB-429", "OPS-12"), ())


def build_observation_plan(
    definition: ServiceDefinition,
    incident: Incident,
    evidence_ids: list[str],
) -> list[PlanStep]:
    if incident.environment not in definition.environment:
        return []
    return [
        PlanStep(
            id=f"step-{step.id}",
            title=step.title,
            objective=step.objective,
            tool_name=step.tool_name,
            tool_input=render_template(step.input_template, incident),
            evidence_ids=evidence_ids,
            success_criteria=list(step.success_criteria),
            rollback=RollbackPlan(
                mode="manual",
                rationale="只读调查不产生业务副作用；失败时停止并检查观测连接。",
            ),
            risk=RiskLevel.LOW,
            rationale=step.rationale,
        )
        for step in definition.observation_steps
    ]


def _matches(check: Check, values: Mapping[str, object]) -> bool:
    actual = values.get(check.field)
    expected = check.value
    try:
        return {
            "eq": lambda: actual == expected,
            "ne": lambda: actual != expected,
            "lt": lambda: actual < expected,
            "lte": lambda: actual <= expected,
            "gt": lambda: actual > expected,
            "gte": lambda: actual >= expected,
            "contains": lambda: expected in actual,
            "in": lambda: actual in expected,
        }[check.operator]()
    except (KeyError, TypeError, ValueError):
        return False


def build_fixture_remediation(
    definition: ServiceDefinition,
    incident: Incident,
    sources: list[SourceHit],
    observations: list[Observation],
) -> ModelProposal | None:
    values = observation_values(observations)
    template = next(
        (
            candidate
            for candidate in definition.fixture_remediations
            if all(_matches(check, values) for check in candidate.match_checks)
        ),
        None,
    )
    if template is None:
        return None
    evidence = [item.id for item in observations]
    document_evidence = [item.chunk_id for item in sources[:3]]
    rollback = RollbackPlan(
        mode=template.rollback_mode,
        tool_name=template.rollback_tool_name,
        tool_input=render_template(
            template.rollback_input_template, incident, values
        ),
        rationale=template.rollback_rationale,
    )
    return ModelProposal(
        phase="remediation",
        diagnosis=template.diagnosis,
        confidence=template.confidence,
        evidence_ids=(evidence + document_evidence)[:12],
        plan=[
            PlanStep(
                id=f"step-{template.id}",
                title=template.title,
                objective=template.objective,
                tool_name=template.tool_name,
                tool_input=render_template(template.input_template, incident, values),
                evidence_ids=evidence,
                preconditions=list(template.preconditions),
                success_criteria=list(template.success_criteria),
                rollback=rollback,
                risk=template.risk,
                rationale=template.rationale,
            )
        ],
        safety_notes=[
            "写动作来自服务注册合同，仍须通过策略编译和真实角色审批。",
            "执行结果必须由服务注册的独立观测工具验证。",
        ],
    )
