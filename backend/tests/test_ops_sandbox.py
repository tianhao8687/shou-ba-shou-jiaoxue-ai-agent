from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app.schemas import UserIdentity
from app.security import AuthService, CapabilityService
import ops_sandbox.app as sandbox


def test_remote_status_snapshot_contains_fresh_write_guard_fields() -> None:
    state = {
        "instances": {"catalog-api-1": "healthy"},
        "metrics": {"stale_sample_rate": 22.0, "p95_ms": 180},
        "cache_version": "v452",
        "db_version": "v453",
    }

    status, _summary, output, effects = sandbox._apply_tool(
        "get_service_status",
        {"service": "catalog-api"},
        "stale_cache",
        state,
    )

    assert status == "succeeded"
    assert effects == 0
    assert output["stale_sample_rate"] == 22.0
    assert output["cache_version"] == "v452"
    assert output["db_version"] == "v453"
    assert output["instances"] == {"catalog-api-1": "healthy"}


def test_http_lab_persists_idempotency_enforces_capability_and_hides_oracle(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(sandbox, "DATABASE_PATH", tmp_path / "fault-lab.db")
    monkeypatch.setattr(sandbox, "FAULT_INJECTION_ENABLED", True)
    capability_secret = "harbor-local-capability-secret-change-me"
    monkeypatch.setattr(sandbox, "CAPABILITY_SECRET", capability_secret)
    health_headers = {"Authorization": f"Bearer {sandbox.HEALTH_TOKEN}"}
    oracle_headers = {"Authorization": f"Bearer {sandbox.ORACLE_TOKEN}"}
    with TestClient(sandbox.app) as client:
        created = client.post(
            "/v1/lab/experiments",
            json={"fault_kind": "queue_backlog"},
            headers=oracle_headers,
        )
        assert created.status_code == 201
        experiment_id = created.json()["experiment_id"]
        payload = {
            "experiment_id": experiment_id,
            "service": "invoice-worker",
            "target_replicas": 6,
            "change_ticket": "CHG-3001",
        }
        actor = UserIdentity(
            username="lead@harbor.local",
            display_name="lead",
            roles=["observer", "operator", "on-call-lead"],
        )
        job_id = "JOB-HTTP-LAB"
        grant = CapabilityService(capability_secret).issue(
            run_id="RUN-HTTP-LAB",
            plan_hash_value="a" * 64,
            step_id="step-scale-consumers",
            tool_name="scale_workers",
            payload=payload,
            actor=actor,
            required_role="on-call-lead",
            job_id=job_id,
            fencing_token=1,
        )
        key = uuid4().hex + uuid4().hex
        headers = {
            "Authorization": f"Bearer {grant.token}",
            "X-Idempotency-Key": key,
            "X-Harbor-Control-Tenant": "xm-ops",
            "X-Harbor-Job-Id": job_id,
            "X-Harbor-Fencing-Token": "1",
            "X-Test-Drop-Response": "after-commit",
        }
        lost = client.post("/v1/tools/scale_workers", json=payload, headers=headers)
        assert lost.status_code == 503
        replay = client.post(
            "/v1/tools/scale_workers",
            json=payload,
            headers={
                "Authorization": f"Bearer {grant.token}",
                "X-Idempotency-Key": key,
                "X-Harbor-Control-Tenant": "xm-ops",
                "X-Harbor-Job-Id": job_id,
                "X-Harbor-Fencing-Token": "1",
            },
        )
        assert replay.status_code == 200
        assert replay.json()["status"] == "skipped"
        assert replay.json()["output"]["replicas"] == 6
        oracle = client.get(
            f"/v1/lab/experiments/{experiment_id}/oracle", headers=oracle_headers
        ).json()
        assert oracle["effects"] == 1
        assert client.get("/health", headers=health_headers).json()["idempotency_records"] == 1

        tampered = {**payload, "target_replicas": 8}
        rejected = client.post(
            "/v1/tools/scale_workers",
            json=tampered,
            headers={
                "Authorization": f"Bearer {grant.token}",
                "X-Idempotency-Key": uuid4().hex + uuid4().hex,
                "X-Harbor-Control-Tenant": "xm-ops",
                "X-Harbor-Job-Id": job_id,
                "X-Harbor-Fencing-Token": "1",
            },
        )
        assert rejected.status_code == 403

        wrong_tenant = client.post(
            "/v1/tools/scale_workers",
            json=payload,
            headers={
                "Authorization": f"Bearer {grant.token}",
                "X-Idempotency-Key": uuid4().hex + uuid4().hex,
                "X-Harbor-Control-Tenant": "other-tenant",
                "X-Harbor-Job-Id": job_id,
                "X-Harbor-Fencing-Token": "1",
            },
        )
        assert wrong_tenant.status_code == 403

        metrics_payload = {
            "experiment_id": experiment_id,
            "service": "invoice-worker",
            "window_minutes": 5,
        }
        newer_grant = CapabilityService(capability_secret).issue(
            run_id="RUN-HTTP-LAB",
            plan_hash_value="b" * 64,
            step_id="step-new-owner-observe",
            tool_name="query_metrics",
            payload=metrics_payload,
            actor=actor,
            required_role="observer",
            job_id=job_id,
            fencing_token=2,
        )
        newer = client.post(
            "/v1/tools/query_metrics",
            json=metrics_payload,
            headers={
                "Authorization": f"Bearer {newer_grant.token}",
                "X-Idempotency-Key": uuid4().hex + uuid4().hex,
                "X-Harbor-Control-Tenant": "xm-ops",
                "X-Harbor-Job-Id": job_id,
                "X-Harbor-Fencing-Token": "2",
            },
        )
        assert newer.status_code == 200

        stale_grant = CapabilityService(capability_secret).issue(
            run_id="RUN-HTTP-LAB",
            plan_hash_value="c" * 64,
            step_id="step-stale-owner-observe",
            tool_name="query_metrics",
            payload=metrics_payload,
            actor=actor,
            required_role="observer",
            job_id=job_id,
            fencing_token=1,
        )
        stale = client.post(
            "/v1/tools/query_metrics",
            json=metrics_payload,
            headers={
                "Authorization": f"Bearer {stale_grant.token}",
                "X-Idempotency-Key": uuid4().hex + uuid4().hex,
                "X-Harbor-Control-Tenant": "xm-ops",
                "X-Harbor-Job-Id": job_id,
                "X-Harbor-Fencing-Token": "1",
            },
        )
        assert stale.status_code == 409
        assert "stale fencing token" in stale.json()["detail"]

        auth_token = AuthService(
            "test-auth-signing-secret-is-long-enough", "password-2026"
        ).issue(actor).access_token
        hidden = client.get(
            f"/v1/lab/experiments/{experiment_id}/oracle",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert hidden.status_code == 403
