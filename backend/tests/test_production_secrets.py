from __future__ import annotations

import pytest

from app.config import Settings, validate_production_settings


def _secure_settings(**overrides) -> Settings:
    values = {
        "app_env": "production",
        "demo_mode": False,
        "demo_password": "random-demo-password-7f6a90c221",
        "auth_signing_secret": "random-auth-signing-8c1b239ee41a",
        "capability_signing_secret": "random-capability-2c09c512fb31",
        "lab_oracle_token": "random-oracle-token-1b1d606357aa",
    }
    values.update(overrides)
    return Settings(**values)


def test_demo_mode_allows_documented_demo_secrets() -> None:
    validate_production_settings(Settings(app_env="local", demo_mode=True))


def test_non_demo_mode_rejects_default_demo_secrets() -> None:
    with pytest.raises(RuntimeError, match="unsafe production secrets") as exc:
        validate_production_settings(Settings(app_env="staging", demo_mode=False))

    assert "auth_signing_secret" in str(exc.value)
    assert "lab_oracle_token" in str(exc.value)


def test_production_environment_rejects_change_me_even_in_demo_mode() -> None:
    settings = _secure_settings(
        demo_mode=True,
        capability_signing_secret="change-me-before-production",
    )

    with pytest.raises(RuntimeError, match="capability_signing_secret"):
        validate_production_settings(settings)


def test_production_environment_accepts_independent_random_secrets() -> None:
    validate_production_settings(_secure_settings())
