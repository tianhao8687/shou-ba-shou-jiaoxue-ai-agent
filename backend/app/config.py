from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    app_name: str = "Harbor AgentOps"
    app_env: str = "local"
    demo_mode: bool = True
    database_url: str = "sqlite:///./harbor.db"
    vector_backend: str = "memory"
    embedding_backend: str = "lexical-feature-baseline"
    embedding_endpoint: str = ""
    embedding_model: str = ""
    embedding_api_key: str = ""
    model_enabled: bool = False
    model_fixture_mode: bool = True
    model_provider: str = "local-openvino"
    model_name: str = "OpenVINO/Qwen3-VL-8B-Instruct-int4-ov"
    model_endpoint: str = "http://127.0.0.1:8091/v1"
    model_api_key: str = "harbor-local-model-token"
    model_timeout_seconds: float = 180.0
    tool_mode: str = "inprocess"
    tool_sandbox_url: str = "http://127.0.0.1:8092"
    tool_sandbox_token: str = "harbor-local-health-token"
    tool_timeout_seconds: float = 8.0
    lab_oracle_token: str = "harbor-local-oracle-token-change-me"
    auth_signing_secret: str = "harbor-local-auth-signing-secret-change-me"
    capability_signing_secret: str = "harbor-local-capability-secret-change-me"
    demo_password: str = "harbor-demo-2026"
    auth_ttl_minutes: int = 480
    capability_ttl_seconds: int = 90
    medium_risk_approval_quorum: int = 1
    high_risk_approval_quorum: int = 2
    enforce_requester_separation: bool = True
    embedded_worker: bool = True
    worker_id: str = "embedded-worker-1"
    worker_poll_seconds: float = 0.25
    worker_lease_seconds: int = 30
    worker_heartbeat_seconds: int = 8
    worker_metrics_port: int = 9101
    frontend_origin: str = "http://localhost:5173"
    data_dir: Path = PROJECT_ROOT / "data"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def allowed_origins(self) -> list[str]:
        origins = {self.frontend_origin, "http://localhost:4173", "http://localhost:8080"}
        return sorted(origins)


@lru_cache
def get_settings() -> Settings:
    return Settings()
