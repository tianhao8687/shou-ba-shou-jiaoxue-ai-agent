from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
for import_root in (BACKEND_ROOT, PROJECT_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from app.config import Settings
from app.main import create_app


def _login(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "test-password-2026"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    data_dir = Path(__file__).resolve().parents[2] / "data"
    settings = Settings(
        app_env="test",
        demo_mode=False,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        vector_backend="memory",
        embedding_backend="lexical-feature-baseline",
        model_enabled=False,
        model_fixture_mode=True,
        tool_mode="inprocess",
        embedded_worker=False,
        frontend_origin="http://testserver",
        data_dir=data_dir,
        auth_signing_secret="test-auth-signing-secret-is-long-enough",
        capability_signing_secret="test-capability-secret-is-long-enough",
        lab_oracle_token="test-oracle-token-is-long-enough",
        demo_password="test-password-2026",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture()
def admin_headers(client: TestClient) -> dict[str, str]:
    return _login(client, "admin@harbor.local")


@pytest.fixture()
def viewer_headers(client: TestClient) -> dict[str, str]:
    return _login(client, "viewer@harbor.local")


@pytest.fixture()
def operator_headers(client: TestClient) -> dict[str, str]:
    return _login(client, "operator@harbor.local")


@pytest.fixture()
def lead_headers(client: TestClient) -> dict[str, str]:
    return _login(client, "lead@harbor.local")


@pytest.fixture()
def security_headers(client: TestClient) -> dict[str, str]:
    return _login(client, "security@harbor.local")


@pytest.fixture()
def approver_headers(client: TestClient) -> dict[str, str]:
    return _login(client, "approver@harbor.local")


@pytest.fixture()
def other_tenant_headers(client: TestClient) -> dict[str, str]:
    return _login(client, "other@harbor.local")
