from __future__ import annotations

from app.config import Settings
from lumen_core.constants import DEFAULT_CHAT_MODEL
from lumen_core.models import Completion


def test_shared_default_chat_model_is_gpt_55() -> None:
    assert DEFAULT_CHAT_MODEL == "gpt-5.5"


def test_completion_model_uses_shared_default() -> None:
    assert Completion.__table__.c.model.default is not None
    assert Completion.__table__.c.model.default.arg == DEFAULT_CHAT_MODEL


def test_api_config_default_chat_model_is_gpt_55() -> None:
    settings = Settings(app_env="dev", upstream_default_model=DEFAULT_CHAT_MODEL)
    assert settings.upstream_default_model == "gpt-5.5"
