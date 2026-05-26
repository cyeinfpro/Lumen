from __future__ import annotations

import inspect

from sqlalchemy.exc import IntegrityError

from lumen_core.models import Conversation, SystemPrompt, User
from lumen_core.schemas import ConversationOut, SystemPromptOut

from app.routes.messages import choose_system_prompt
from app.routes import system_prompts


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


def test_system_prompt_integrity_uses_structured_constraint_name() -> None:
    class Diag:
        constraint_name = "uq_system_prompts_user_name"

    class Orig(Exception):
        diag = Diag()

        def __repr__(self) -> str:
            return "different-message"

    http = system_prompts._classify_integrity(  # noqa: SLF001
        IntegrityError("insert", {}, Orig())
    )

    assert http.status_code == 409
    assert http.detail["error"]["code"] == "duplicate_name"


def test_system_prompt_audit_uses_caller_transaction() -> None:
    source = inspect.getsource(system_prompts)

    for event_type in [
        "system_prompt.create",
        "system_prompt.update",
        "system_prompt.delete",
    ]:
        start = source.index(f'event_type="{event_type}"')
        end = source.index("await db.commit()", start)
        assert "autocommit=False" in source[start:end]
