from __future__ import annotations

import sys
from pathlib import Path

import pytest

TG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TG_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]

from app.handlers.generation import GenerationRuntime  # noqa: E402
from app.keyboards import DEFAULT_PARAMS  # noqa: E402
from app.listener import ListenerRuntimeState  # noqa: E402


def test_listener_runtime_instances_do_not_share_mutable_state() -> None:
    first = ListenerRuntimeState()
    second = ListenerRuntimeState()

    first.progress_last_edit["gen-1"] = 1.0
    first.chat_send_locks[1] = object()  # type: ignore[assignment]
    first.chat_send_next_at[1] = 2.0

    assert second.progress_last_edit == {}
    assert second.chat_send_locks == {}
    assert second.chat_send_next_at == {}
    assert first.dispatch_semaphore is not second.dispatch_semaphore


def test_generation_runtime_instances_do_not_share_auth_log_state() -> None:
    first = GenerationRuntime()
    second = GenerationRuntime()

    first.heartbeat_auth_logged.add(100)

    assert second.heartbeat_auth_logged == set()


def test_default_generation_params_are_immutable() -> None:
    with pytest.raises(TypeError):
        DEFAULT_PARAMS["count"] = 2  # type: ignore[index]
