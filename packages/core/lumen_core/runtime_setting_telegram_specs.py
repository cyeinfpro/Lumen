"""Telegram-specific runtime setting metadata."""

from __future__ import annotations

from .runtime_setting_types import SettingSpec


TELEGRAM_PROXY_ENDPOINT_SETTINGS: tuple[SettingSpec, ...] = (
    SettingSpec(
        key="telegram.proxy_bind_host",
        description=(
            "Telegram 选中 SSH 代理时，本机动态 SOCKS 监听地址。"
            "默认 127.0.0.1，仅当前进程所在主机可访问；"
            "容器部署可设为 0.0.0.0，但不要把动态端口发布到宿主机。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="TELEGRAM_PROXY_BIND_HOST",
    ),
    SettingSpec(
        key="telegram.proxy_advertise_host",
        description=(
            "Telegram runtime-config 返回给独立 bot 进程的 SSH SOCKS 主机名。"
            "默认与监听地址一致；容器部署应使用仅内部网络可解析的 API 服务名。"
        ),
        sensitive=False,
        parser=str,
        env_fallback="TELEGRAM_PROXY_ADVERTISE_HOST",
    ),
)
