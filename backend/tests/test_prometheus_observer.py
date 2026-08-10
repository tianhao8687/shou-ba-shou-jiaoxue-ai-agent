from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.tools import (
    PrometheusReadOnlyClient,
    PrometheusServiceInput,
    RoutingToolClient,
)


def _install_observer(client: TestClient, handler) -> None:
    lab_client = client.app.state.tool_executor.client
    observer = PrometheusReadOnlyClient(
        "http://prometheus.test",
        "",
        2,
        client.app.state.capabilities,
        transport=httpx.MockTransport(handler),
    )
    client.app.state.tool_executor.client = RoutingToolClient(lab_client, observer)
    client.app.state.engine.production_observation_enabled = True


def _start_production_run(
    client: TestClient, admin_headers: dict[str, str], suffix: str
) -> str:
    created = client.post(
        "/api/runs",
        headers=admin_headers,
        json={
            "incident": {
                "title": f"payment-api {suffix}",
                "summary": "生产支付接口需要通过真实监控完成只读取证。",
                "severity": "P1",
                "service": "payment-api",
                "environment": "production",
                "symptoms": ["5xx 错误率持续升高"],
            }
        },
    )
    assert created.status_code == 202, created.text
    return created.json()["id"]


def test_production_incident_collects_real_prometheus_evidence_then_hands_off(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    queries: list[str] = []

    def prometheus(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="Prometheus is Ready.")
        assert request.url.path == "/api/v1/query"
        query = request.url.params["query"]
        queries.append(query)
        value = (
            "780"
            if "histogram_quantile" in query
            else "2.4"
            if "status=~" in query
            else "1"
            if query.startswith("min(up")
            else "126"
        )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{"metric": {}, "value": [1_786_000_000, value]}],
                },
            },
        )

    _install_observer(client, prometheus)
    run_id = _start_production_run(client, admin_headers, "生产错误率持续升高")

    assert client.app.state.worker.run_once() is True
    run = client.get(f"/api/runs/{run_id}", headers=admin_headers).json()

    assert run["status"] == "handed_off"
    assert len(run["observations"]) == 1
    assert len(run["model_calls"]) == 1
    assert run["observations"][0]["tool_name"] == "query_prometheus_slo"
    assert run["observations"][0]["data"]["sample_count"] == 4
    assert run["observations"][0]["source_uri"] == "http://prometheus.test/api/v1/query"
    assert run["plan"] == []
    assert "未配置生产写连接器" in run["resolution"]
    assert len(queries) == 4
    assert all('service="payment-api"' in query for query in queries)
    assert all("生产支付接口" not in query for query in queries)


def test_empty_prometheus_result_stops_before_model_or_write(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    def prometheus(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="ready")
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "vector", "result": []},
            },
        )

    _install_observer(client, prometheus)
    run_id = _start_production_run(client, admin_headers, "空指标安全停止")

    assert client.app.state.worker.run_once() is True
    run = client.get(f"/api/runs/{run_id}", headers=admin_headers).json()

    assert run["status"] == "handed_off"
    assert run["observations"][0]["data"]["sample_count"] == 0
    assert run["model_calls"] == []
    assert run["plan"] == []
    assert [item["tool_name"] for item in run["tool_results"]] == [
        "query_prometheus_slo"
    ]
    assert "没有返回可归因指标" in run["resolution"]


@pytest.mark.parametrize("failure", ["unauthorized", "timeout"])
def test_prometheus_transport_failures_stop_before_model_or_write(
    client: TestClient,
    admin_headers: dict[str, str],
    failure: str,
) -> None:
    def prometheus(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="ready")
        if failure == "timeout":
            raise httpx.ReadTimeout("simulated Prometheus timeout", request=request)
        return httpx.Response(401, json={"status": "error", "error": "unauthorized"})

    _install_observer(client, prometheus)
    run_id = _start_production_run(client, admin_headers, f"{failure} 安全停止")

    assert client.app.state.worker.run_once() is True
    run = client.get(f"/api/runs/{run_id}", headers=admin_headers).json()

    assert run["status"] == "handed_off"
    assert run["observations"] == []
    assert run["model_calls"] == []
    assert run["plan"] == []
    assert len(run["tool_results"]) == 1
    assert run["tool_results"][0]["tool_name"] == "query_prometheus_slo"
    assert run["tool_results"][0]["status"] == "unknown"
    assert "只读观测失败" in run["resolution"]


def test_prometheus_input_rejects_caller_supplied_promql() -> None:
    with pytest.raises(ValidationError):
        PrometheusServiceInput.model_validate(
            {
                "service": "payment-api",
                "environment": "production",
                "window_minutes": 15,
                "query": "up or vector(1)",
            }
        )
