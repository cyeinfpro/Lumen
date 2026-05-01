from __future__ import annotations

from lumen_core.models import Conversation, SystemPrompt, User
from lumen_core.schemas import ConversationOut, SystemPromptOut

from app.routes.messages import choose_system_prompt


def test_system_prompt_model_has_owner_content_and_default_references() -> None:
    assert SystemPrompt.__tablename__ == "system_prompts"
    assert "user_id" in SystemPrompt.__table__.c
    assert "name" in SystemPrompt.__table__.c
    assert "content" in SystemPrompt.__table__.c
    assert "default_system_prompt_id" in User.__table__.c
    assert "default_system_prompt_id" in Conversation.__table__.c


def test_prompt_and_conversation_schemas_expose_prompt_selection() -> None:
    assert "id" in SystemPromptOut.model_fields
    assert "name" in SystemPromptOut.model_fields
    assert "content" in SystemPromptOut.model_fields
    assert "is_default" in SystemPromptOut.model_fields
    assert "default_system_prompt_id" in ConversationOut.model_fields


def test_choose_system_prompt_prefers_explicit_then_conversation_then_global() -> None:
    assert (
        choose_system_prompt(
            explicit_prompt="per send",
            conversation_prompt="conversation",
            legacy_conversation_prompt="legacy",
            global_prompt="global",
        )
        == "per send"
    )
    assert (
        choose_system_prompt(
            explicit_prompt="   ",
            conversation_prompt="conversation",
            legacy_conversation_prompt="legacy",
            global_prompt="global",
        )
        == "conversation"
    )
    assert (
        choose_system_prompt(
            explicit_prompt=None,
            conversation_prompt=None,
            legacy_conversation_prompt="legacy",
            global_prompt="global",
        )
        == "legacy"
    )
    assert (
        choose_system_prompt(
            explicit_prompt=None,
            conversation_prompt=None,
            legacy_conversation_prompt=None,
            global_prompt="global",
        )
        == "global"
    )
