from __future__ import annotations

import ast
from pathlib import Path

from app.routes import messages


ROUTES_ROOT = Path(__file__).resolve().parents[1] / "app" / "routes"
FACADE_PATH = ROUTES_ROOT / "messages.py"
PARTS_ROOT = ROUTES_ROOT / "messages_parts"
EXPECTED_PARTS = {
    "__init__.py",
    "memory.py",
    "publishing.py",
    "queries.py",
    "silent.py",
    "submission.py",
}
COMPATIBILITY_SEAMS = {
    "AssistantTaskResult",
    "DEFAULT_IMAGE_OUTPUT_FORMAT",
    "SilentGenerationIn",
    "SilentGenerationOut",
    "_apply_explicit_memory_write",
    "_apply_pending_confirmation_reply",
    "_await_post_commit_publishes",
    "_create_assistant_task",
    "_lock_idempotency_key",
    "_lookup_idempotent_post",
    "_lookup_silent_generation",
    "_publish_assistant_task",
    "_publish_message_appended",
    "_resolve_task_credential_pin",
    "_silent_generation_request_hash",
    "await_post_commit_publish",
    "await_post_commit_publishes",
    "create_assistant_task",
    "idempotency_lookup_keys",
    "message_alive_filters",
    "publish_assistant_task",
    "publish_message_appended",
    "resolve_system_prompt_for_message",
    "submit_user_message",
}


def test_messages_facade_and_parts_stay_within_line_budgets() -> None:
    assert len(FACADE_PATH.read_text(encoding="utf-8").splitlines()) <= 800
    part_paths = sorted(PARTS_ROOT.glob("*.py"))
    assert {path.name for path in part_paths} == EXPECTED_PARTS
    oversized = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in part_paths
        if len(path.read_text(encoding="utf-8").splitlines()) > 600
    }
    assert oversized == {}


def test_messages_parts_do_not_reverse_import_facade() -> None:
    violations: list[tuple[str, int]] = []
    for path in PARTS_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name == "app.routes.messages" for alias in node.names):
                    violations.append((path.name, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                imports_facade_module = node.module == "app.routes.messages"
                imports_facade_name = (
                    node.level > 0
                    and node.module in {None, ""}
                    and any(alias.name == "messages" for alias in node.names)
                )
                imports_facade_path = (
                    node.level > 0
                    and node.module is not None
                    and node.module.endswith("messages")
                )
                if imports_facade_module or imports_facade_name or imports_facade_path:
                    violations.append((path.name, node.lineno))
    assert violations == []


def test_messages_facade_preserves_compatibility_seams() -> None:
    missing = sorted(
        name for name in COMPATIBILITY_SEAMS if not hasattr(messages, name)
    )
    assert missing == []
