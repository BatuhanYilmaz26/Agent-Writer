"""Fail-closed configuration and secret redaction behavior."""

import json

import pytest
from pydantic import ValidationError

from agentwriter.settings import Settings


def test_local_fixture_defaults_are_loopback_and_no_external_calls() -> None:
    settings = Settings(_env_file=None)
    assert settings.bind_host == "127.0.0.1"
    assert settings.auth_mode == "local"
    assert settings.model_provider == settings.research_provider == "fixture"
    assert not settings.enable_external_calls


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.1", "example.org"])
def test_local_auth_cannot_bind_public_interface(host: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(_env_file=None, bind_host=host)


def test_hosted_mode_cannot_silently_use_development_defaults() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="staging", development_mode=False, auth_mode="oidc")


def test_external_provider_requires_qualification_and_key() -> None:
    with pytest.raises(ValidationError, match="qualification"):
        Settings(_env_file=None, model_provider="openrouter", openrouter_api_key="secret", enable_external_calls=True)
    settings = Settings(
        _env_file=None, model_provider="openrouter", openrouter_api_key="secret",
        enable_external_calls=True, openrouter_qualified=True,
    )
    assert settings.model_provider == "openrouter"
    assert settings.openrouter_api_key is not None


def test_canonical_env_names_and_no_legacy_key_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "canonical-value")
    monkeypatch.setenv("OPENROUTER_KEY", "legacy-value")
    monkeypatch.setenv("MAX_MODEL_CALLS", "7")
    settings = Settings(_env_file=None)
    assert settings.openrouter_api_key is not None
    assert settings.openrouter_api_key.get_secret_value() == "canonical-value"
    assert settings.max_model_calls == 7


def test_secret_values_never_enter_repr_json_or_summary() -> None:
    secret = "do-not-log-very-private-credential"
    settings = Settings(_env_file=None, openrouter_api_key=secret, database_url="postgresql://owner:secret@localhost/agentwriter")
    assert secret not in repr(settings)
    assert secret not in settings.model_dump_json()
    summary = json.dumps(settings.sanitized_summary())
    assert secret not in summary
    assert "postgresql://" not in summary
    assert "openrouter_api_key" not in summary


@pytest.mark.parametrize("change", [
    {"job_lease_seconds": 30, "job_heartbeat_seconds": 20},
    {"database_pool_min_size": 6, "database_pool_max_size": 5},
    {"allowed_origins": ("*",)}, {"allowed_hosts": ("*",)},
    {"openrouter_base_url": "https://attacker.invalid/api/v1"},
    {"treg_base_url": "https://attacker.invalid"},
    {"model_policy": "free_only", "openrouter_model": "paid/model", "model_allowlist": ("paid/model",)},
    {"model_policy": "budgeted"}, {"langgraph_strict_msgpack": False},
    {"source_retention_days": 31}, {"max_review_rounds": 3},
])
def test_incompatible_resource_and_provider_policy_fails(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **change)


def test_invalid_database_dsn_is_redacted_in_error() -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, database_url="invalid://sensitive-password")
    assert "sensitive-password" not in str(exc.value)


def test_dotenv_is_loaded_only_when_named(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "settings.env"
    path.write_text("MAX_MODEL_CALLS=9\n", encoding="utf-8")
    assert Settings(_env_file=path).max_model_calls == 9
