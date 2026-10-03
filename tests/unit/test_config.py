import pytest
from pydantic import SecretStr, ValidationError

from jev_invoice.config import Settings


def test_defaults_pin_an_exact_jev_model() -> None:
    settings = Settings(_env_file=None)
    assert settings.typesafe_model != "jev-latest"


def test_secrets_are_masked_in_repr() -> None:
    settings = Settings(typesafe_api_key=SecretStr("ts-secret-value"), _env_file=None)
    assert "ts-secret-value" not in repr(settings)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_deployed_envs_require_api_keys(env: str) -> None:
    with pytest.raises(ValidationError, match="TYPESAFE_API_KEY"):
        Settings(app_env=env, _env_file=None)


def test_production_accepts_complete_settings() -> None:
    settings = Settings(
        app_env="production",
        typesafe_api_key=SecretStr("ts"),
        groq_api_key=SecretStr("gq"),
        bank_fingerprint_key=SecretStr("prod-fingerprint-key"),
        _env_file=None,
    )
    assert settings.app_env == "production"
