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
)


__all__ = ["AGENT_SETTINGS"]
