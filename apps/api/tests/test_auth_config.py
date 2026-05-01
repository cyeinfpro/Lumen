from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.scripts import bootstrap
from app.config import Settings, _settings_env_files
from lumen_core.runtime_settings import validate_providers


def _prod_kwargs() -> dict[str, str]:
    return {
        "app_env": "prod",
        "session_secret": "x" * 32,
        "database_url": "postgresql+asyncpg://lumen_prod:secret@localhost:5432/lumen",
        "redis_url": "redis://:redis-prod-password@localhost:6379/0",
    }


def test_non_dev_requires_explicit_session_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(ValidationError):
        Settings(app_env="prod", _env_file=None)


def test_non_dev_rejects_placeholder_session_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(app_env="prod", session_secret="change-me")


def test_non_dev_rejects_short_session_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(app_env="production", session_secret="short")


def test_dev_allows_example_session_secret() -> None:
    settings = Settings(app_env="dev", session_secret="change-me")
    assert settings.session_secret == "change-me"


def test_dev_default_session_secret_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    settings = Settings(app_env="dev", _env_file=None)
    assert settings.session_secret == ""


def test_non_dev_no_longer_requires_upstream_api_key() -> None:
    settings = Settings(**_prod_kwargs())
    assert settings.app_env == "prod"


def test_non_dev_rejects_default_postgres_credentials() -> None:
    kwargs = _prod_kwargs()
    kwargs["database_url"] = "postgresql+asyncpg://lumen:lumen@localhost:5432/lumen"

    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_non_dev_rejects_default_redis_password() -> None:
    kwargs = _prod_kwargs()
    kwargs["redis_url"] = "redis://:lumen-redis-dev-password@localhost:6379/0"

    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_settings_env_files_support_explicit_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    explicit_env = tmp_path / "custom.env"
    monkeypatch.setenv("LUMEN_ENV_FILE", str(explicit_env))

    assert _settings_env_files()[-1] == explicit_env


def test_providers_accept_http_base_url() -> None:
    raw = (
        '[{"name":"internal",'
        '"base_url":"http://10.0.0.8:8000/v1",'
        '"api_key":"sk"}]'
    )

    assert validate_providers(raw) == raw


def test_providers_reject_missing_api_key() -> None:
    with pytest.raises(ValueError):
        validate_providers(
            '[{"name":"bad","base_url":"https://api.example.com","api_key":""}]'
        )


@pytest.mark.asyncio
async def test_bootstrap_rejects_explicit_empty_password(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = await bootstrap.main(["admin@example.com", "--password", ""])

    assert rc == 2
    assert "--password must not be empty" in capsys.readouterr().err
