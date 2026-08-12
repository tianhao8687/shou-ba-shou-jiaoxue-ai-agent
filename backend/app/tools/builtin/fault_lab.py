from __future__ import annotations

from copy import deepcopy
import math
import threading
from typing import Any
from uuid import uuid4

from ...schemas import Incident
from ..contracts import ToolCallResponse

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
        # Onboarded tools register handlers through the platform boundary.  The lab
        # remains unaware of service names and tool-specific business logic.
        from ...registry.tools import LAB_TOOL_HANDLERS

        extension = LAB_TOOL_HANDLERS.get(tool_name)
        if extension is not None:
            return extension(experiment, payload)
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
