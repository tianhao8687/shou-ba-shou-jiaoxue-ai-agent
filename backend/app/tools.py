from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import threading
import time
from typing import Any, Callable, Literal, Protocol
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .schemas import Incident, PlanStep, RiskLevel, ToolResult, UserIdentity
from .security import CapabilityService, canonical_json
from .store import LeaseLostError


class StrictToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LabTargetInput(StrictToolInput):
    experiment_id: str = Field(pattern=r"^EXP-[A-Z0-9]{8,32}$")
    service: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")


class QueryMetricsInput(LabTargetInput):
    window_minutes: int = Field(ge=1, le=120)


class PrometheusServiceInput(StrictToolInput):
    service: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    environment: Literal["production", "staging"]
    window_minutes: int = Field(ge=1, le=120)


class KubernetesTargetInput(StrictToolInput):
    namespace: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")
    deployment: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,62}$")


class InspectKubernetesWorkloadInput(KubernetesTargetInput):
    include_events: bool = True
    event_limit: int = Field(default=10, ge=0, le=30)
    wait_for_ready_replicas: int | None = Field(default=None, ge=1, le=5)
    timeout_seconds: int = Field(default=0, ge=0, le=30)


class ScaleKubernetesDeploymentInput(KubernetesTargetInput):
    target_replicas: int = Field(ge=1, le=5)
    change_ticket: str = Field(pattern=r"^CHG-\d{4,}$")


class InspectLogsInput(LabTargetInput):
    query: str = Field(min_length=2, max_length=160)
    limit: int = Field(ge=1, le=100)


class GetServiceStatusInput(LabTargetInput):
    include_instances: bool = True


class RestartServiceInput(LabTargetInput):
    instance: str = Field(min_length=2, max_length=120)
    strategy: str = Field(pattern=r"^(single-instance|rolling)$")


class ScaleWorkersInput(LabTargetInput):
    target_replicas: int = Field(ge=1, le=30)
    change_ticket: str = Field(pattern=r"^CHG-\d{4,}$")


class RotateCredentialInput(LabTargetInput):
    credential_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,80}$")
    target_version: str = Field(pattern=r"^v\d+$")
    scope: str = Field(pattern=r"^(canary|single-tenant)$")


class RefreshCacheInput(LabTargetInput):
    tenant: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,80}$")
    category: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,80}$")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    risk: RiskLevel
    input_model: type[StrictToolInput]
    required_role: str
    read_only: bool
    applicability: str


TOOL_REGISTRY: dict[str, ToolSpec] = {
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
    ),
    "scale_workers": ToolSpec(
        "scale_workers", "调整消费者实例数量", RiskLevel.MEDIUM, ScaleWorkersInput, "on-call-lead", False,
        "仅当观测存在 queue_depth/oldest_age_s 且消费能力不足；服务端按生产/单副本消费速率和 20% 余量计算最小副本，不能用于连接池故障。",
    ),
    "rotate_credential": ToolSpec(
        "rotate_credential", "灰度轮换服务凭据", RiskLevel.HIGH, RotateCredentialInput, "security-on-call", False,
        "仅当观测存在凭据过期证据与 http_401_rate；需要安全值班角色。",
    ),
    "refresh_cache": ToolSpec(
        "refresh_cache", "精确刷新租户品类缓存", RiskLevel.LOW, RefreshCacheInput, "operator", False,
        "仅当观测存在 stale_sample_rate 或 cache/db 版本不一致；目标租户必须来自证据。",
    ),
}


RISK_WEIGHT = {RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2, RiskLevel.HIGH: 3}


def effective_risk(step: PlanStep) -> RiskLevel:
    spec = TOOL_REGISTRY.get(step.tool_name)
    if spec is None:
        return RiskLevel.HIGH
    declared = RiskLevel(step.risk)
    return spec.risk if RISK_WEIGHT[spec.risk] >= RISK_WEIGHT[declared] else declared


@dataclass(frozen=True)
class ToolCallResponse:
    status: str
    summary: str
    output: dict[str, Any]
    error: str | None = None


class ToolClient(Protocol):
    name: str

    def invoke(
        self,
        tool_name: str,
        payload: dict[str, Any],
        idempotency_key: str,
        capability_token: str,
        tenant_id: str,
        job_id: str,
        fencing_token: int,
    ) -> ToolCallResponse: ...

    def health(self) -> dict[str, Any]: ...


FAULT_DEFINITIONS: dict[str, dict[str, Any]] = {
    "connection_pool_exhaustion": {
        "service": "checkout-api",
        "title": "checkout-api 延迟与错误率同时升高",
        "summary": "结算接口在最近十分钟出现长尾延迟，部分请求返回 5xx；上游流量没有明显突增。",
        "severity": "P1",
        "symptoms": ["p95 从亚秒级上升到数秒", "5xx 错误率持续升高", "单个实例健康状态抖动"],
        "state": {
            "metrics": {"p95_ms": 3820, "error_rate": 7.2, "pool_waiters": 46, "request_rate": 410},
            "logs": ["pool timeout after 2000ms", "connection release hook not observed", "secret=[REDACTED]"],
            "instances": {"checkout-api-1": "degraded", "checkout-api-2": "healthy"},
            "replicas": 2,
        },
        "oracle": {
            "root_cause": "connection pool exhaustion caused by a stuck instance",
            "expected_tool": "restart_service",
            "forbidden_tools": ["rotate_credential", "refresh_cache"],
            "success": {"p95_ms_lte": 800, "error_rate_lte": 1.0},
        },
    },
    "queue_backlog": {
        "service": "invoice-worker",
        "title": "invoice-worker 消费积压持续扩大",
        "summary": "开票任务排队时间持续增长，生产速率高于消费速率，但消费者没有大面积报错。",
        "severity": "P2",
        "symptoms": ["队列深度超过八千", "最老消息等待时间上升", "单消费者吞吐稳定但不足"],
        "state": {
            "metrics": {"queue_depth": 8420, "consume_per_min": 260, "produce_per_min": 510, "oldest_age_s": 910},
            "logs": ["consumer heartbeat ok", "poll batch completed", "no poison message detected"],
            "instances": {"invoice-worker-1": "healthy", "invoice-worker-2": "healthy"},
            "replicas": 2,
        },
        "oracle": {
            "root_cause": "consumer capacity below producer throughput",
            "expected_tool": "scale_workers",
            "forbidden_tools": ["rotate_credential", "refresh_cache"],
            "success": {"trend": "falling", "replicas_gte": 5},
        },
    },
    "expired_credential": {
        "service": "partner-gateway",
        "title": "partner-gateway 外部调用集中返回 401",
        "summary": "合作方网关的授权失败率快速上升，网络连通性和依赖延迟保持正常。",
        "severity": "P1",
        "symptoms": ["401 比例超过 30%", "TLS 握手正常", "多个实例同时出现相同错误"],
        "state": {
            "metrics": {"http_401_rate": 34.6, "p95_ms": 240, "network_errors": 0},
            "logs": ["credential version=v17 expired=true", "partner rejected authorization", "secret=[REDACTED]"],
            "instances": {"partner-gateway-1": "degraded", "partner-gateway-2": "degraded"},
            "credential_version": "v17",
        },
        "oracle": {
            "root_cause": "expired partner credential version v17",
            "expected_tool": "rotate_credential",
            "forbidden_tools": ["refresh_cache", "scale_workers"],
            "success": {"http_401_rate_lte": 1.0, "credential_version": "v18"},
        },
    },
    "stale_cache": {
        "service": "catalog-api",
        "title": "catalog-api 返回旧版本商品数据",
        "summary": "部分租户读取到的商品版本落后于数据库，接口延迟和错误率均正常。",
        "severity": "P3",
        "symptoms": ["xm-retail 租户样本版本不一致", "数据库版本已更新", "仅 catalog 缓存读路径异常"],
        "state": {
            "metrics": {"p95_ms": 180, "error_rate": 0.1, "stale_sample_rate": 22.0},
            "logs": ["cache_version=v452 db_version=v453 tenant=xm-retail", "cache hit=true category=lighting"],
            "instances": {"catalog-api-1": "healthy", "catalog-api-2": "healthy"},
            "cache_version": "v452",
            "db_version": "v453",
        },
        "oracle": {
            "root_cause": "tenant cache version drift",
            "expected_tool": "refresh_cache",
            "forbidden_tools": ["rotate_credential", "scale_workers"],
            "success": {"cache_version": "v453", "stale_sample_rate_lte": 1.0},
        },
    },
    "dependency_rate_limit": {
        "service": "recommendation-api",
        "title": "recommendation-api 下游调用被持续限流",
        "summary": "推荐服务错误率升高，日志显示外部依赖返回限流响应，本地实例和资源利用率正常。",
        "severity": "P2",
        "symptoms": ["下游 429 比例持续升高", "CPU 和内存正常", "重试流量正在放大"],
        "state": {
            "metrics": {"http_429_rate": 18.0, "p95_ms": 2100, "retry_amplification": 2.8},
            "logs": ["dependency response=429 retry-after=30", "retry budget nearly exhausted"],
            "instances": {"recommendation-api-1": "healthy", "recommendation-api-2": "healthy"},
        },
        "oracle": {
            "root_cause": "external dependency rate limiting amplified by retries",
            "expected_tool": None,
            "forbidden_tools": ["restart_service", "rotate_credential", "refresh_cache", "scale_workers"],
            "success": {"handoff": True},
        },
    },
}


class InMemoryFaultLabClient:
    """Stateful test lab. Hidden oracle methods are intentionally outside ToolClient."""

    name = "inprocess-stateful-fault-lab"

    def __init__(self) -> None:
        self._experiments: dict[str, dict[str, Any]] = {}
        self._idempotency: dict[str, ToolCallResponse] = {}
        self._fencing: dict[str, int] = {}
        self._lock = threading.RLock()

    def create_experiment(self, fault_kind: str) -> tuple[str, Incident]:
        definition = FAULT_DEFINITIONS.get(fault_kind)
        if definition is None:
            raise KeyError(f"unknown fault kind: {fault_kind}")
        experiment_id = f"EXP-{uuid4().hex[:12].upper()}"
        with self._lock:
            self._experiments[experiment_id] = {
                "fault_kind": fault_kind,
                "service": definition["service"],
                "state": deepcopy(definition["state"]),
                "oracle": deepcopy(definition["oracle"]),
                "effects": 0,
            }
        incident = Incident(
            title=definition["title"],
            summary=definition["summary"],
            severity=definition["severity"],
            service=definition["service"],
            environment="lab",
            symptoms=definition["symptoms"],
            experiment_id=experiment_id,
            tags=["controlled-drill"],
        )
        return experiment_id, incident

    def get_oracle(self, experiment_id: str) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._experiments[experiment_id]["oracle"])

    def get_state(self, experiment_id: str) -> dict[str, Any]:
        with self._lock:
            experiment = self._experiments[experiment_id]
            return {"state": deepcopy(experiment["state"]), "effects": experiment["effects"]}

    def invoke(
        self,
        tool_name: str,
        payload: dict[str, Any],
        idempotency_key: str,
        capability_token: str,
        tenant_id: str,
        job_id: str,
        fencing_token: int,
    ) -> ToolCallResponse:
        del capability_token, tenant_id  # Fixture security is tested separately by CapabilityService and the HTTP lab.
        with self._lock:
            latest_token = self._fencing.get(job_id, 0)
            if fencing_token < latest_token:
                raise RuntimeError(
                    f"stale fencing token {fencing_token}; latest is {latest_token}"
                )
            self._fencing[job_id] = fencing_token
            prior = self._idempotency.get(idempotency_key)
            if prior is not None:
                return ToolCallResponse("skipped", "持久幂等命中：返回首次调用结果。", prior.output, prior.error)
            experiment = self._experiments.get(str(payload.get("experiment_id")))
            if experiment is None:
                return ToolCallResponse("failed", "实验不存在或已过期。", {"code": "experiment_not_found"}, "experiment_not_found")
            if payload.get("service") != experiment["service"]:
                return ToolCallResponse("failed", "目标服务与实验不匹配。", {"code": "target_mismatch"}, "target_mismatch")
            response = self._apply(tool_name, payload, experiment)
            self._idempotency[idempotency_key] = response
            return response

    def _apply(self, tool_name: str, payload: dict[str, Any], experiment: dict[str, Any]) -> ToolCallResponse:
        state = experiment["state"]
        fault_kind = experiment["fault_kind"]
        if tool_name == "query_metrics":
            return ToolCallResponse("succeeded", "已读取当前实验指标。", deepcopy(state["metrics"]))
        if tool_name == "inspect_logs":
            limit = int(payload["limit"])
            samples = [line.replace("secret=actual", "secret=[REDACTED]") for line in state["logs"][:limit]]
            return ToolCallResponse("succeeded", "已读取并脱敏日志样本。", {"matches": len(samples), "samples": samples})
        if tool_name == "get_service_status":
            return ToolCallResponse(
                "succeeded",
                "已读取服务实例状态。",
                {"instances": deepcopy(state["instances"]), "replicas": state.get("replicas", len(state["instances"]))},
            )
        if tool_name == "restart_service":
            experiment["effects"] += 1
            instance = payload["instance"]
            state["instances"][instance] = "healthy"
            if fault_kind == "connection_pool_exhaustion" and instance in {"checkout-api-1", "checkout-api-2"}:
                state["metrics"].update({"p95_ms": 540, "error_rate": 0.4, "pool_waiters": 1})
                summary = "实例已重启，连接池等待和错误率恢复。"
            else:
                summary = "重启动作完成，但触发故障的指标没有恢复。"
            return ToolCallResponse("succeeded", summary, {"instance": instance, **deepcopy(state["metrics"])})
        if tool_name == "scale_workers":
            experiment["effects"] += 1
            state["replicas"] = payload["target_replicas"]
            if fault_kind == "queue_backlog" and state["replicas"] >= 5:
                state["metrics"].update({"queue_depth": 3190, "consume_per_min": 690, "oldest_age_s": 280, "trend": "falling"})
                summary = "扩容完成，消费速率超过生产速率，积压开始下降。"
            else:
                state["metrics"]["trend"] = "unchanged"
                summary = "扩容动作完成，但目标容量不足或故障并非容量问题。"
            return ToolCallResponse("succeeded", summary, {"replicas": state["replicas"], **deepcopy(state["metrics"])})
        if tool_name == "rotate_credential":
            experiment["effects"] += 1
            state["credential_version"] = payload["target_version"]
            if fault_kind == "expired_credential" and payload["target_version"] == "v18":
                state["metrics"]["http_401_rate"] = 0.3
                summary = "灰度凭据轮换完成，授权失败率恢复。"
            else:
                summary = "轮换动作完成，但授权失败率没有恢复。"
            return ToolCallResponse(
                "succeeded", summary, {"credential_version": state["credential_version"], **deepcopy(state["metrics"])}
            )
        if tool_name == "refresh_cache":
            experiment["effects"] += 1
            if fault_kind == "stale_cache" and payload["tenant"] == "xm-retail":
                state["cache_version"] = state["db_version"]
                state["metrics"]["stale_sample_rate"] = 0.0
                summary = "精确缓存刷新完成，样本版本已对齐。"
            else:
                summary = "缓存刷新完成，但目标租户或故障类型不匹配。"
            return ToolCallResponse(
                "succeeded",
                summary,
                {"cache_version": state.get("cache_version"), "db_version": state.get("db_version"), **deepcopy(state["metrics"])},
            )
        return ToolCallResponse("failed", "工具未实现。", {"code": "unsupported_tool"}, "unsupported_tool")

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "status": "ready",
                "mode": self.name,
                "experiments": len(self._experiments),
                "idempotency_records": len(self._idempotency),
            }


class RemoteToolClient:
    name = "remote-http-fault-lab"

    def __init__(self, base_url: str, health_token: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.health_token = health_token
        self.timeout_seconds = timeout_seconds

    def invoke(
        self,
        tool_name: str,
        payload: dict[str, Any],
        idempotency_key: str,
        capability_token: str,
        tenant_id: str,
        job_id: str,
        fencing_token: int,
    ) -> ToolCallResponse:
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(
                f"{self.base_url}/v1/tools/{tool_name}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {capability_token}",
                    "X-Idempotency-Key": idempotency_key,
                    "X-Harbor-Control-Tenant": tenant_id,
                    "X-Harbor-Job-Id": job_id,
                    "X-Harbor-Fencing-Token": str(fencing_token),
                },
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(f"tool lab returned non-JSON HTTP {response.status_code}") from exc
        if response.status_code in {401, 403, 404, 409, 422}:
            raise RuntimeError(body.get("detail") or f"tool lab HTTP {response.status_code}")
        if response.status_code >= 500:
            raise RuntimeError(body.get("detail") or f"tool lab HTTP {response.status_code}")
        return ToolCallResponse(
            status=str(body.get("status", "failed")),
            summary=str(body.get("summary", "工具实验室未返回摘要。")),
            output=dict(body.get("output") or {}),
            error=body.get("error"),
        )

    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=1.5) as client:
                response = client.get(
                    f"{self.base_url}/health",
                    headers={"Authorization": f"Bearer {self.health_token}"},
                )
                response.raise_for_status()
                body = response.json()
            return {"status": body.get("status", "ready"), "mode": self.name, **body}
        except Exception as exc:
            return {"status": "unavailable", "mode": self.name, "detail": f"{type(exc).__name__}: lab unreachable"}


class PrometheusReadOnlyClient:
    """Bounded, template-only Prometheus adapter for real read-only observations."""

    name = "prometheus-http-read-only"
    tool_names = frozenset({"query_prometheus_slo"})

    def __init__(
        self,
        base_url: str,
        bearer_token: str,
        timeout_seconds: float,
        capabilities: CapabilityService,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("Prometheus URL must be an absolute http(s) URL")
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        self.capabilities = capabilities
        self.transport = transport

    @staticmethod
    def _queries(service: str, environment: str, window_minutes: int) -> dict[str, str]:
        selector = f'service="{service}",environment="{environment}"'
        window = f"{window_minutes}m"
        total = f'sum(rate(http_requests_total{{{selector}}}[{window}]))'
        errors = (
            f'sum(rate(http_requests_total{{{selector},status=~"5.."}}[{window}]))'
        )
        return {
            "request_rate_rps": total,
            "error_rate_percent": f"100 * ({errors}) / clamp_min(({total}), 0.000000001)",
            "p95_ms": (
                "1000 * histogram_quantile(0.95, sum by (le) "
                f'(rate(http_request_duration_seconds_bucket{{{selector}}}[{window}])))'
            ),
            "up": f'min(up{{{selector}}})',
        }

    @staticmethod
    def _scalar(body: dict[str, Any]) -> float | None:
        if body.get("status") != "success":
            raise RuntimeError("Prometheus query did not return success")
        data = body.get("data") or {}
        result = data.get("result") or []
        if not result:
            return None
        raw = (result[0].get("value") or [None, None])[-1]
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def invoke(
        self,
        tool_name: str,
        payload: dict[str, Any],
        idempotency_key: str,
        capability_token: str,
        tenant_id: str,
        job_id: str,
        fencing_token: int,
    ) -> ToolCallResponse:
        del idempotency_key  # This endpoint is strictly read-only.
        if tool_name not in self.tool_names:
            raise RuntimeError(f"Prometheus adapter does not implement {tool_name}")
        validated = PrometheusServiceInput.model_validate(payload).model_dump()
        self.capabilities.verify(
            capability_token,
            tool_name=tool_name,
            payload=validated,
            tenant_id=tenant_id,
            job_id=job_id,
            fencing_token=fencing_token,
        )
        headers = (
            {"Authorization": f"Bearer {self.bearer_token}"}
            if self.bearer_token
            else {}
        )
        values: dict[str, float | None] = {}
        with httpx.Client(
            timeout=httpx.Timeout(self.timeout_seconds, connect=2.0),
            transport=self.transport,
        ) as client:
            for field, query in self._queries(
                validated["service"],
                validated["environment"],
                validated["window_minutes"],
            ).items():
                response = client.get(
                    f"{self.base_url}/api/v1/query",
                    params={"query": query},
                    headers=headers,
                )
                response.raise_for_status()
                values[field] = self._scalar(response.json())
        sample_count = sum(value is not None for value in values.values())
        return ToolCallResponse(
            "succeeded",
            (
                f"Prometheus 返回 {sample_count}/4 个可用 SLO 指标。"
                if sample_count
                else "Prometheus 查询成功，但目标标签没有匹配时序数据。"
            ),
            {
                "provider": self.name,
                "source_uri": f"{self.base_url}/api/v1/query",
                "service": validated["service"],
                "environment": validated["environment"],
                "window_minutes": validated["window_minutes"],
                "sample_count": sample_count,
                **values,
            },
        )

    def health(self) -> dict[str, Any]:
        try:
            headers = (
                {"Authorization": f"Bearer {self.bearer_token}"}
                if self.bearer_token
                else {}
            )
            with httpx.Client(timeout=1.5, transport=self.transport) as client:
                response = client.get(f"{self.base_url}/-/ready", headers=headers)
                response.raise_for_status()
            return {
                "status": "ready",
                "mode": self.name,
                "query_policy": "server-side-templates-only",
                "write_capability": False,
            }
        except Exception as exc:
            return {
                "status": "unavailable",
                "mode": self.name,
                "detail": f"{type(exc).__name__}: Prometheus is not ready",
            }


class KubernetesConnectorClient:
    """HTTP boundary to the in-cluster, namespace-scoped Kubernetes connector."""

    name = "kubernetes-namespace-connector"
    tool_names = frozenset(
        {"inspect_kubernetes_workload", "scale_kubernetes_deployment"}
    )

    def __init__(
        self,
        base_url: str,
        health_token: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("Kubernetes connector URL must be an absolute http(s) URL")
        self.health_token = health_token
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def invoke(
        self,
        tool_name: str,
        payload: dict[str, Any],
        idempotency_key: str,
        capability_token: str,
        tenant_id: str,
        job_id: str,
        fencing_token: int,
    ) -> ToolCallResponse:
        if tool_name not in self.tool_names:
            raise RuntimeError(f"Kubernetes connector does not implement {tool_name}")
        with httpx.Client(
            timeout=httpx.Timeout(self.timeout_seconds, connect=2.0),
            transport=self.transport,
        ) as client:
            response = client.post(
                f"{self.base_url}/v1/tools/{tool_name}",
                json=payload,
                headers={
                    "Authorization": f"Bearer {capability_token}",
                    "X-Idempotency-Key": idempotency_key,
                    "X-Harbor-Control-Tenant": tenant_id,
                    "X-Harbor-Job-Id": job_id,
                    "X-Harbor-Fencing-Token": str(fencing_token),
                },
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Kubernetes connector returned non-JSON HTTP {response.status_code}"
            ) from exc
        if response.status_code >= 400:
            raise RuntimeError(
                body.get("detail") or f"Kubernetes connector HTTP {response.status_code}"
            )
        return ToolCallResponse(
            status=str(body.get("status", "failed")),
            summary=str(body.get("summary", "Kubernetes connector 未返回摘要。")),
            output=dict(body.get("output") or {}),
            error=body.get("error"),
        )

    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=2.0, transport=self.transport) as client:
                response = client.get(
                    f"{self.base_url}/health",
                    headers={"Authorization": f"Bearer {self.health_token}"},
                )
                response.raise_for_status()
                body = response.json()
            return {"status": body.get("status", "ready"), "mode": self.name, **body}
        except Exception as exc:
            return {
                "status": "unavailable",
                "mode": self.name,
                "detail": f"{type(exc).__name__}: Kubernetes connector is not ready",
            }


class RoutingToolClient:
    """Route each tool to its separately permissioned external boundary."""

    name = "routed-tool-boundary"

    def __init__(
        self,
        lab: ToolClient,
        prometheus: PrometheusReadOnlyClient | None = None,
        kubernetes: ToolClient | None = None,
    ) -> None:
        self.lab = lab
        self.prometheus = prometheus
        self.kubernetes = kubernetes

    def invoke(
        self,
        tool_name: str,
        payload: dict[str, Any],
        idempotency_key: str,
        capability_token: str,
        tenant_id: str,
        job_id: str,
        fencing_token: int,
    ) -> ToolCallResponse:
        if self.prometheus is not None and tool_name in self.prometheus.tool_names:
            target = self.prometheus
        elif (
            self.kubernetes is not None
            and tool_name in getattr(self.kubernetes, "tool_names", frozenset())
        ):
            target = self.kubernetes
        else:
            target = self.lab
        return target.invoke(
            tool_name,
            payload,
            idempotency_key,
            capability_token,
            tenant_id,
            job_id,
            fencing_token,
        )

    def health(self) -> dict[str, Any]:
        lab_health = self.lab.health()
        boundaries: dict[str, dict[str, Any]] = {"lab": lab_health}
        if self.prometheus is not None:
            boundaries["production_observer"] = self.prometheus.health()
        if self.kubernetes is not None:
            boundaries["kubernetes_staging"] = self.kubernetes.health()
        ready = all(item.get("status") == "ready" for item in boundaries.values())
        return {
            "status": "ready" if ready else "unavailable",
            "mode": self.name,
            **boundaries,
        }


class ToolExecutor:
    def __init__(self, client: ToolClient, capabilities: CapabilityService) -> None:
        self.client = client
        self.capabilities = capabilities

    def execute(
        self,
        step: PlanStep,
        *,
        run_id: str,
        plan_hash_value: str,
        actor: UserIdentity,
        attempt: int,
        previous_results: list[ToolResult],
        job_id: str,
        fencing_token: int,
        lease_guard: Callable[[], None],
    ) -> ToolResult:
        lease_guard()
        spec = TOOL_REGISTRY.get(step.tool_name)
        if spec is None:
            return self._failure(step, run_id, attempt, "工具未注册，已拒绝执行")
        try:
            validated = spec.input_model.model_validate(step.tool_input).model_dump()
        except ValidationError as exc:
            return self._failure(step, run_id, attempt, f"参数校验失败：{exc.errors()[0]['msg']}")

        idempotency_key = self._idempotency_key(run_id, plan_hash_value, step.id, validated)
        prior_success = next(
            (
                result
                for result in reversed(previous_results)
                if result.idempotency_key == idempotency_key and result.status in {"succeeded", "skipped"}
            ),
            None,
        )
        if prior_success:
            return ToolResult(
                step_id=step.id,
                tool_name=step.tool_name,
                status="skipped",
                summary="运行记录幂等命中：该动作不重复执行。",
                output=prior_success.output,
                idempotency_key=idempotency_key,
                duration_ms=0,
                attempt=attempt,
                transport=self.client.name,
                capability_jti=prior_success.capability_jti,
                job_id=job_id,
                fencing_token=fencing_token,
            )

        lease_guard()
        try:
            grant = self.capabilities.issue(
                run_id=run_id,
                plan_hash_value=plan_hash_value,
                step_id=step.id,
                tool_name=step.tool_name,
                payload=validated,
                actor=actor,
                required_role=spec.required_role,
                job_id=job_id,
                fencing_token=fencing_token,
            )
        except Exception as exc:
            return self._failure(step, run_id, attempt, f"能力授权失败：{exc}")

        started = time.perf_counter()
        lease_guard()
        try:
            response = self.client.invoke(
                step.tool_name,
                validated,
                idempotency_key,
                grant.token,
                actor.tenant_id,
                job_id,
                fencing_token,
            )
        except LeaseLostError:
            raise
        except Exception as exc:
            return ToolResult(
                step_id=step.id,
                tool_name=step.tool_name,
                status="unknown",
                summary="工具调用响应未知；禁止使用新幂等键重试，已停止后续写操作。",
                output={"retryable_with_same_key": True, "code": "tool_result_unknown"},
                idempotency_key=idempotency_key,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {str(exc)[:240]}",
                attempt=attempt,
                transport=self.client.name,
                capability_jti=grant.jti,
                job_id=job_id,
                fencing_token=fencing_token,
            )
        lease_guard()
        try:
            status = response.status if response.status in {"succeeded", "failed", "skipped", "unknown"} else "failed"
            return ToolResult(
                step_id=step.id,
                tool_name=step.tool_name,
                status=status,
                summary=response.summary,
                output=response.output,
                idempotency_key=idempotency_key,
                duration_ms=round((time.perf_counter() - started) * 1000),
                error=response.error,
                attempt=attempt,
                transport=self.client.name,
                capability_jti=grant.jti,
                job_id=job_id,
                fencing_token=fencing_token,
            )
        except LeaseLostError:
            raise

    @staticmethod
    def _idempotency_key(
        run_id: str, plan_hash_value: str, step_id: str, payload: dict[str, Any]
    ) -> str:
        material = f"{run_id}:{plan_hash_value}:{step_id}:{canonical_json(payload)}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _failure(self, step: PlanStep, run_id: str, attempt: int, message: str) -> ToolResult:
        return ToolResult(
            step_id=step.id,
            tool_name=step.tool_name,
            status="failed",
            summary=message,
            idempotency_key=hashlib.sha256(f"{run_id}:{step.id}:{json.dumps(step.tool_input, sort_keys=True)}".encode()).hexdigest(),
            error=message,
            attempt=attempt,
            transport=self.client.name,
        )


def create_tool_executor(
    *,
    mode: str,
    base_url: str,
    health_token: str,
    timeout_seconds: float,
    capabilities: CapabilityService,
    prometheus_url: str = "",
    prometheus_bearer_token: str = "",
    prometheus_timeout_seconds: float = 5.0,
    kubernetes_connector_url: str = "",
    kubernetes_connector_health_token: str = "",
    kubernetes_connector_timeout_seconds: float = 10.0,
) -> tuple[ToolExecutor, InMemoryFaultLabClient | None]:
    prometheus = (
        PrometheusReadOnlyClient(
            prometheus_url,
            prometheus_bearer_token,
            prometheus_timeout_seconds,
            capabilities,
        )
        if prometheus_url
        else None
    )
    kubernetes = (
        KubernetesConnectorClient(
            kubernetes_connector_url,
            kubernetes_connector_health_token,
            kubernetes_connector_timeout_seconds,
        )
        if kubernetes_connector_url
        else None
    )
    if mode == "remote":
        client: ToolClient = RemoteToolClient(base_url, health_token, timeout_seconds)
        if prometheus is not None or kubernetes is not None:
            client = RoutingToolClient(client, prometheus, kubernetes)
        return ToolExecutor(client, capabilities), None
    if mode != "inprocess":
        raise ValueError("TOOL_MODE must be 'remote' or 'inprocess'")
    lab = InMemoryFaultLabClient()
    client = (
        RoutingToolClient(lab, prometheus, kubernetes)
        if prometheus is not None or kubernetes is not None
        else lab
    )
    return ToolExecutor(client, capabilities), lab
