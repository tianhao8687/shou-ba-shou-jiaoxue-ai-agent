from __future__ import annotations

import hashlib
import json
import os
from uuid import uuid4

import httpx
import pytest

from app.schemas import UserIdentity
from app.security import CapabilityService


OPS_URL = os.getenv("HARBOR_TEST_OPS_URL")


@pytest.mark.skipif(
    not OPS_URL,
    reason="set HARBOR_TEST_OPS_URL to an isolated fault-lab instance",
)
def test_committed_write_is_not_repeated_after_response_loss() -> None:
    """Exercise persistent idempotency and capability checks over real HTTP."""
    assert OPS_URL is not None
    oracle_token = os.environ["HARBOR_TEST_OPS_ORACLE_TOKEN"]
    capability_secret = os.environ["HARBOR_TEST_CAPABILITY_SECRET"]
    tenant_id = f"integration-{uuid4().hex[:10]}"
    actor = UserIdentity(
        username="lead@harbor.local",
        display_name="Integration lead",
        roles=["observer", "operator", "on-call-lead"],
        tenant_id=tenant_id,
    )

    with httpx.Client(base_url=OPS_URL, timeout=10.0) as client:
        created = client.post(
            "/v1/lab/experiments",
            json={"fault_kind": "queue_backlog"},
            headers={"Authorization": f"Bearer {oracle_token}"},
        )
        assert created.status_code == 201, created.text
        experiment_id = created.json()["experiment_id"]
        payload = {
            "experiment_id": experiment_id,
            "service": "invoice-worker",
            "target_replicas": 6,
            "change_ticket": "CHG-9001",
        }
        plan_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        grant = CapabilityService(capability_secret).issue(
            run_id=f"RUN-LIVE-{uuid4().hex[:12].upper()}",
            plan_hash_value=plan_hash,
            step_id="step-scale-consumers",
            tool_name="scale_workers",
            payload=payload,
            actor=actor,
            required_role="on-call-lead",
        )
        idempotency_key = uuid4().hex + uuid4().hex
        base_headers = {
            "Authorization": f"Bearer {grant.token}",
            "X-Idempotency-Key": idempotency_key,
            "X-Harbor-Control-Tenant": tenant_id,
        }

        lost = client.post(
            "/v1/tools/scale_workers",
            json=payload,
            headers={**base_headers, "X-Test-Drop-Response": "after-commit"},
        )
        assert lost.status_code == 503, lost.text

        replay = client.post(
            "/v1/tools/scale_workers", json=payload, headers=base_headers
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["status"] == "skipped"
        assert replay.json()["output"]["replicas"] == 6

        oracle = client.get(
            f"/v1/lab/experiments/{experiment_id}/oracle",
            headers={"Authorization": f"Bearer {oracle_token}"},
        )
        assert oracle.status_code == 200
        assert oracle.json()["effects"] == 1

        tampered = client.post(
            "/v1/tools/scale_workers",
            json={**payload, "target_replicas": 8},
            headers={
                **base_headers,
                "X-Idempotency-Key": uuid4().hex + uuid4().hex,
            },
        )
        assert tampered.status_code == 403

        wrong_tenant = client.post(
            "/v1/tools/scale_workers",
            json=payload,
            headers={
                **base_headers,
                "X-Idempotency-Key": uuid4().hex + uuid4().hex,
                "X-Harbor-Control-Tenant": "wrong-tenant",
            },
        )
        assert wrong_tenant.status_code == 403
