"""Shared conservative capability defaults for Agent chat models."""

from __future__ import annotations

from collections.abc import Iterable


DEFAULT_AGENT_CONTEXT_WINDOW = 128_000
GPT_56_AGENT_CONTEXT_WINDOW = 272_000


def canonical_model_id(model_id: str) -> str:
    value = str(model_id or "").strip().lower()
    return value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def is_gpt_56_model(model_id: str) -> bool:
    return canonical_model_id(model_id).startswith("gpt-5.6")


def default_agent_context_window(model_id: str) -> int:
    if is_gpt_56_model(model_id):
        return GPT_56_AGENT_CONTEXT_WINDOW
    return DEFAULT_AGENT_CONTEXT_WINDOW


def default_agent_context_window_for_models(model_ids: Iterable[str]) -> int:
    models = tuple(model_ids)
    if models and all(is_gpt_56_model(model_id) for model_id in models):
        return GPT_56_AGENT_CONTEXT_WINDOW
    return DEFAULT_AGENT_CONTEXT_WINDOW


__all__ = [
    "DEFAULT_AGENT_CONTEXT_WINDOW",
    "GPT_56_AGENT_CONTEXT_WINDOW",
    "canonical_model_id",
    "default_agent_context_window",
    "default_agent_context_window_for_models",
    "is_gpt_56_model",
]
