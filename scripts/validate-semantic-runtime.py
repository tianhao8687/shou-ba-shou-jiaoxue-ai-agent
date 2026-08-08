from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import time

import httpx
from fastapi.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings
from app.main import create_app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate the real embedding sidecar through the complete API runtime"
    )
    parser.add_argument("--endpoint", default="http://127.0.0.1:8093/v1")
    parser.add_argument(
        "--model", default="OpenVINO/Qwen3-Embedding-0.6B-int4-cw-ov"
    )
    parser.add_argument("--api-key", default="harbor-local-embedding-token")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "docs" / "semantic-runtime-evidence-v3.1.json",
    )
    args = parser.parse_args()

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="harbor-semantic-") as temp_dir:
        settings = Settings(
            app_env="semantic-integration",
            demo_mode=False,
            database_url=f"sqlite:///{Path(temp_dir) / 'semantic.db'}",
            vector_backend="memory",
            embedding_backend="openai-compatible",
            embedding_endpoint=args.endpoint,
            embedding_model=args.model,
            embedding_api_key=args.api_key,
            model_enabled=False,
            model_fixture_mode=True,
            tool_mode="inprocess",
            embedded_worker=False,
            data_dir=PROJECT_ROOT / "data",
            auth_signing_secret="semantic-test-auth-signing-secret-long-enough",
            capability_signing_secret="semantic-test-capability-secret-long-enough",
            demo_password="semantic-test-password",
        )
        with TestClient(create_app(settings)) as client:
            health = client.get("/api/health").json()
            assert health["vector_quality"] == "semantic", health

            login = client.post(
                "/api/auth/login",
                json={
                    "username": "admin@harbor.local",
                    "password": "semantic-test-password",
                },
            )
            login.raise_for_status()
            headers = {
                "Authorization": f"Bearer {login.json()['access_token']}"
            }
            drill = client.post(
                "/api/drills",
                json={"fault_kind": "stale_cache"},
                headers=headers,
            )
            drill.raise_for_status()
            run = client.post(
                "/api/runs",
                json={"incident": drill.json()["incident"]},
                headers=headers,
            )
            run.raise_for_status()
            assert client.app.state.worker.run_once() is True
            completed = client.get(
                f"/api/runs/{run.json()['id']}", headers=headers
            ).json()
            assert completed["status"] == "completed", completed
            evidence = client.get(
                f"/api/runs/{completed['id']}/evidence", headers=headers
            ).json()
            assert evidence["contract"]["vector_quality"] == "semantic"
            assert completed["sources"]

    sidecar_health = httpx.get(
        f"{args.endpoint.rstrip('/').removesuffix('/v1')}/v1/health", timeout=5.0
    ).json()
    report = {
        "schema": "harbor-semantic-runtime/v3.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "api_health": {
            "version": health["version"],
            "vector_backend": health["vector_backend"],
            "vector_quality": health["vector_quality"],
        },
        "embedding_runtime": sidecar_health,
        "end_to_end_run": {
            "run_id": completed["id"],
            "tenant_id": completed["tenant_id"],
            "status": completed["status"],
            "source_documents": sorted(
                {source["doc_id"] for source in completed["sources"]}
            ),
            "retrieval_channels": sorted(
                {source["retrieval_channel"] for source in completed["sources"]}
            ),
            "selected_tools": [
                result["tool_name"] for result in completed["tool_results"]
            ],
            "evidence_leak_check": evidence["runtime_leak_check"],
        },
        "passed": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
