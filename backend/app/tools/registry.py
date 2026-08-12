from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import replace

from ..schemas import PlanStep, RiskLevel, RunRecord
from .contracts import (
    GetServiceStatusInput,
    InspectKubernetesWorkloadInput,
    InspectLogsInput,
    PreExecutionObservationSpec,
    PrometheusServiceInput,
    QueryMetricsInput,
    RefreshCacheInput,
    RestartServiceInput,
    RotateCredentialInput,
    ScaleKubernetesDeploymentInput,
    ScaleWorkersInput,
    ToolCompensation,
    ToolSpec,
)


def _lab_service_status_payload(
    record: RunRecord, step: PlanStep
) -> dict[str, object]:
    experiment_id = step.tool_input.get("experiment_id") or record.incident.experiment_id
    service = step.tool_input.get("service") or record.incident.service
    if not isinstance(experiment_id, str) or not experiment_id:
        raise ValueError("lab write requires an experiment_id for fresh observation")
    if not isinstance(service, str) or not service:
        raise ValueError("lab write requires a service for fresh observation")
    return {
        "experiment_id": experiment_id,
        "service": service,
        "include_instances": True,
    }


def _kubernetes_workload_payload(
    _record: RunRecord, step: PlanStep
) -> dict[str, object]:
    return {
        "namespace": step.tool_input.get("namespace"),
        "deployment": step.tool_input.get("deployment"),
        "include_events": False,
        "event_limit": 0,
        "wait_for_ready_replicas": None,
        "timeout_seconds": 10,
    }


def _scale_workers_compensation(
    _record: RunRecord, step: PlanStep, before_state: dict[str, object]
) -> ToolCompensation:
    replicas = before_state.get("replicas")
    if not isinstance(replicas, int) or isinstance(replicas, bool):
        raise ValueError("fresh state does not contain an integer replicas value")
    payload = {
        "experiment_id": step.tool_input["experiment_id"],
        "service": step.tool_input["service"],
        "target_replicas": replicas,
        "change_ticket": step.tool_input["change_ticket"],
    }
    return ToolCompensation(
        tool_name="scale_workers",
        payload=payload,
        before_state={"replicas": replicas},
        expected_state={"replicas": replicas},
    )


def _scale_kubernetes_compensation(
    _record: RunRecord, step: PlanStep, before_state: dict[str, object]
) -> ToolCompensation:
    replicas = before_state.get("replicas")
    if not isinstance(replicas, int) or isinstance(replicas, bool):
        raise ValueError("fresh state does not contain an integer replicas value")
    payload = {
        "namespace": step.tool_input["namespace"],
        "deployment": step.tool_input["deployment"],
        "target_replicas": replicas,
        "change_ticket": step.tool_input["change_ticket"],
    }
    return ToolCompensation(
        tool_name="scale_kubernetes_deployment",
        payload=payload,
        before_state={
            "namespace": before_state.get("namespace"),
            "deployment": before_state.get("deployment"),
            "replicas": replicas,
        },
        expected_state={"replicas": replicas},
    )


LAB_STATUS_FOR_RESTART = PreExecutionObservationSpec(
    tool_name="get_service_status",
    payload_builder=_lab_service_status_payload,
    required_fields=frozenset({"instances"}),
)
LAB_STATUS_FOR_SCALE = PreExecutionObservationSpec(
    tool_name="get_service_status",
    payload_builder=_lab_service_status_payload,
    required_fields=frozenset({"replicas"}),
)
LAB_STATUS_FOR_CREDENTIAL = PreExecutionObservationSpec(
    tool_name="get_service_status",
    payload_builder=_lab_service_status_payload,
    required_fields=frozenset({"credential_version"}),
)
LAB_STATUS_FOR_CACHE = PreExecutionObservationSpec(
    tool_name="get_service_status",
    payload_builder=_lab_service_status_payload,
    required_fields=frozenset({"cache_version", "db_version"}),
)
KUBERNETES_STATE_FOR_SCALE = PreExecutionObservationSpec(
    tool_name="inspect_kubernetes_workload",
    payload_builder=_kubernetes_workload_payload,
    required_fields=frozenset({"namespace", "deployment", "replicas"}),
)


class ToolRegistry(Mapping[str, ToolSpec]):
    """Single registration boundary used by policy, prompts and execution."""

    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec, *, replace: bool = False) -> None:
        if spec.name in self._specs and not replace:
            raise ValueError(f"tool already registered: {spec.name}")
        self._specs[spec.name] = spec

    def unregister(self, name: str) -> None:
        self._specs.pop(name)

    def __getitem__(self, name: str) -> ToolSpec:
        return self._specs[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

_BUILTIN_TOOL_SPECS: dict[str, ToolSpec] = {
    "query_prometheus_slo": ToolSpec(
        "query_prometheus_slo",
        "通过 Prometheus HTTP API 读取生产服务 SLO 指标",
        RiskLevel.LOW,
        PrometheusServiceInput,
        "observer",
        True,
        "生产或预发布事件的只读基础观测；PromQL 由服务端模板生成，调用方不能提交任意查询。",
    ),
    "inspect_kubernetes_workload": ToolSpec(
        "inspect_kubernetes_workload",
        "读取隔离 Kubernetes namespace 中的 Deployment、Pod、事件和容量信号",
        RiskLevel.LOW,
        InspectKubernetesWorkloadInput,
        "observer",
        True,
        "仅用于配置好的 staging namespace 与 Deployment 白名单；不能读取 Secret 或执行 Pod 命令。",
    ),
    "scale_kubernetes_deployment": ToolSpec(
        "scale_kubernetes_deployment",
        "通过 Kubernetes deployments/scale 子资源调整白名单 Deployment 副本数",
        RiskLevel.MEDIUM,
        ScaleKubernetesDeploymentInput,
        "on-call-lead",
        False,
        "仅用于隔离 staging namespace；副本范围 1–5，不能修改镜像、命令、ServiceAccount 或 Pod 模板。",
        pre_execution_observations=(KUBERNETES_STATE_FOR_SCALE,),
    ),
    "query_metrics": ToolSpec(
        "query_metrics", "读取实验服务的时序指标快照", RiskLevel.LOW, QueryMetricsInput, "observer", True,
        "所有已绑定实验的基础调查；只读。",
    ),
    "inspect_logs": ToolSpec(
        "inspect_logs", "读取并脱敏实验服务日志", RiskLevel.LOW, InspectLogsInput, "observer", True,
        "所有需要区分错误类型的基础调查；只读并脱敏。",
    ),
    "get_service_status": ToolSpec(
        "get_service_status", "读取服务与实例状态", RiskLevel.LOW, GetServiceStatusInput, "observer", True,
        "写操作前确认具体实例与当前副本；只读。",
    ),
    "restart_service": ToolSpec(
        "restart_service", "滚动重启指定实例", RiskLevel.HIGH, RestartServiceInput, "on-call-lead", False,
        "仅当观测显示具体实例异常或连接池等待/超时；必须使用观测到的实例名。",
        pre_execution_observations=(LAB_STATUS_FOR_RESTART,),
    ),
    "scale_workers": ToolSpec(
        "scale_workers", "调整消费者实例数量", RiskLevel.MEDIUM, ScaleWorkersInput, "on-call-lead", False,
        "仅当观测存在 queue_depth/oldest_age_s 且消费能力不足；服务端按生产/单副本消费速率和 20% 余量计算最小副本，不能用于连接池故障。",
        pre_execution_observations=(LAB_STATUS_FOR_SCALE,),
    ),
    "rotate_credential": ToolSpec(
        "rotate_credential", "灰度轮换服务凭据", RiskLevel.HIGH, RotateCredentialInput, "security-on-call", False,
        "仅当观测存在凭据过期证据与 http_401_rate；需要安全值班角色。",
        pre_execution_observations=(LAB_STATUS_FOR_CREDENTIAL,),
    ),
    "refresh_cache": ToolSpec(
        "refresh_cache", "精确刷新租户品类缓存", RiskLevel.LOW, RefreshCacheInput, "operator", False,
        "仅当观测存在 stale_sample_rate 或 cache/db 版本不一致；目标租户必须来自证据。",
        pre_execution_observations=(LAB_STATUS_FOR_CACHE,),
    ),
}

# Safety semantics live with the tool contract instead of in the policy compiler.
_BUILTIN_TOOL_SPECS.update(
    {
        "query_prometheus_slo": replace(
            _BUILTIN_TOOL_SPECS["query_prometheus_slo"],
            rollback_contract="none",
            idempotency_policy="read-only",
        ),
        "inspect_kubernetes_workload": replace(
            _BUILTIN_TOOL_SPECS["inspect_kubernetes_workload"],
            rollback_contract="none",
            idempotency_policy="read-only",
        ),
        "scale_kubernetes_deployment": replace(
            _BUILTIN_TOOL_SPECS["scale_kubernetes_deployment"],
            rollback_contract="same-tool",
            compensatable=True,
            compensation_builder=_scale_kubernetes_compensation,
            required_observation_fields=frozenset(
                {"queue_depth", "replicas", "recommended_replicas"}
            ),
        ),
        "query_metrics": replace(
            _BUILTIN_TOOL_SPECS["query_metrics"],
            rollback_contract="none",
            idempotency_policy="read-only",
        ),
        "inspect_logs": replace(
            _BUILTIN_TOOL_SPECS["inspect_logs"],
            rollback_contract="none",
            idempotency_policy="read-only",
        ),
        "get_service_status": replace(
            _BUILTIN_TOOL_SPECS["get_service_status"],
            rollback_contract="none",
            idempotency_policy="read-only",
        ),
        "restart_service": replace(
            _BUILTIN_TOOL_SPECS["restart_service"],
            required_observation_fields=frozenset({"pool_waiters", "instances"}),
        ),
        "scale_workers": replace(
            _BUILTIN_TOOL_SPECS["scale_workers"],
            rollback_contract="same-tool",
            compensatable=True,
            compensation_builder=_scale_workers_compensation,
            required_observation_fields=frozenset({"queue_depth", "oldest_age_s"}),
        ),
        "rotate_credential": replace(
            _BUILTIN_TOOL_SPECS["rotate_credential"],
            required_observation_fields=frozenset({"http_401_rate"}),
        ),
        "refresh_cache": replace(
            _BUILTIN_TOOL_SPECS["refresh_cache"],
            required_observation_fields=frozenset(
                {"stale_sample_rate", "cache_version", "db_version"}
            ),
        ),
    }
)


TOOL_REGISTRY = ToolRegistry(_BUILTIN_TOOL_SPECS.values())

RISK_WEIGHT = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}


def effective_risk(step: PlanStep) -> RiskLevel:
    spec = TOOL_REGISTRY.get(step.tool_name)
    if spec is None:
        return RiskLevel.HIGH
    declared = RiskLevel(step.risk)
    return spec.risk if RISK_WEIGHT[spec.risk] >= RISK_WEIGHT[declared] else declared
