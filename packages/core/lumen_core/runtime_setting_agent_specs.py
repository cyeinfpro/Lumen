"""Closed-by-default Agent runtime setting specifications."""

from __future__ import annotations

from .runtime_setting_types import SettingSpec


AGENT_SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="agent.enabled",
        description="Agent API capability gate. 0=disabled, 1=enabled.",
        sensitive=False,
        parser=int,
        env_fallback="AGENT_ENABLED",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="ui.nav.agent_visible",
        description="Whether to show Agent in primary navigation. 0=hidden, 1=shown.",
        sensitive=False,
        parser=int,
        env_fallback="UI_NAV_AGENT_VISIBLE",
        min_value=0,
        max_value=1,
        allowed_values=("0", "1"),
    ),
    SettingSpec(
        key="agent.max_turns",
        description="Maximum model turns in one Agent run.",
        sensitive=False,
        parser=int,
        env_fallback="AGENT_MAX_TURNS",
        min_value=1,
        max_value=12,
    ),
    SettingSpec(
        key="agent.max_tool_calls",
        description="Maximum total tool calls in one Agent run.",
        sensitive=False,
        parser=int,
        env_fallback="AGENT_MAX_TOOL_CALLS",
        min_value=0,
        max_value=12,
    ),
    SettingSpec(
        key="agent.max_image_tool_calls",
        description="Maximum image tool calls in one Agent run.",
        sensitive=False,
        parser=int,
        env_fallback="AGENT_MAX_IMAGE_TOOL_CALLS",
        min_value=0,
        max_value=8,
    ),
    SettingSpec(
        key="agent.max_images_per_run",
        description="Maximum image generations submitted by one Agent run.",
        sensitive=False,
        parser=int,
        env_fallback="AGENT_MAX_IMAGES_PER_RUN",
        min_value=1,
        max_value=16,
    ),
    SettingSpec(
        key="agent.max_reference_images",
        description="Maximum reference images explicitly attached to one Agent message.",
        sensitive=False,
        parser=int,
        env_fallback="AGENT_MAX_REFERENCE_IMAGES",
        min_value=0,
        max_value=16,
    ),
    SettingSpec(
        key="agent.max_session_images",
        description="Maximum readable image resources retained in one Agent session.",
        sensitive=False,
        parser=int,
        env_fallback="AGENT_MAX_SESSION_IMAGES",
        min_value=16,
        max_value=64,
    ),
    SettingSpec(
        key="agent.max_output_tokens",
        description="Per-turn output token ceiling used for Agent reservation.",
        sensitive=False,
        parser=int,
        env_fallback="AGENT_MAX_OUTPUT_TOKENS",
        min_value=256,
        max_value=32000,
    ),
    SettingSpec(
        key="agent.run_timeout_seconds",
        description="Wall-clock timeout for one Agent run.",
        sensitive=False,
        parser=int,
        env_fallback="AGENT_RUN_TIMEOUT_SECONDS",
        min_value=10,
        max_value=1800,
    ),
    SettingSpec(
        key="agent.tool_timeout_seconds",
        description="Timeout for one Agent tool gateway request.",
        sensitive=False,
        parser=int,
        env_fallback="AGENT_TOOL_TIMEOUT_SECONDS",
        min_value=5,
        max_value=300,
    ),
    SettingSpec(
        key="agent.capability_ttl_seconds",
        description="Lifetime of run-scoped Agent tool capabilities.",
        sensitive=False,
        parser=int,
        env_fallback="AGENT_CAPABILITY_TTL_SECONDS",
        min_value=15,
        max_value=600,
    ),
)


__all__ = ["AGENT_SETTINGS"]
