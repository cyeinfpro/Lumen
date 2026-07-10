from __future__ import annotations

import json

from lumen_core.video_providers import (
    parse_video_provider_config_json,
    parse_video_provider_item,
    select_video_provider,
)


def _provider_raw(**overrides):
    raw = {
        "name": "volcano-main",
        "kind": "volcano",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3/",
        "api_key": "ark-key",
        "enabled": True,
        "priority": 10,
        "weight": 2,
        "concurrency": 3,
        "supports_idempotency": True,
        "models": {
            "seedance-2.0:t2v": "doubao-seedance-2-0",
            "seedance-2.0:i2v": "doubao-seedance-2-0-i2v",
            "seedance-2.0:reference": "doubao-seedance-2-0-ref",
        },
    }
    raw.update(overrides)
    return raw


def test_parse_video_provider_item_normalizes_and_maps_actions() -> None:
    provider = parse_video_provider_item(_provider_raw(), index=0)

    assert provider.name == "volcano-main"
    assert provider.base_url == "https://ark.cn-beijing.volces.com/api/v3"
    assert provider.priority == 10
    assert provider.weight == 2
    assert provider.concurrency == 3
    assert provider.supports_idempotency is True
    assert provider.supports("seedance-2.0", "t2v")
    assert provider.supports("seedance-2.0", "i2v")
    assert provider.supports("seedance-2.0", "reference")
    assert provider.upstream_model_for("seedance-2.0", "t2v") == "doubao-seedance-2-0"
    assert (
        provider.upstream_model_for("seedance-2.0", "i2v") == "doubao-seedance-2-0-i2v"
    )
    assert (
        provider.upstream_model_for("seedance-2.0", "reference")
        == "doubao-seedance-2-0-ref"
    )


def test_parse_volcano_provider_rewrites_byteplus_seedance_mini_alias() -> None:
    provider = parse_video_provider_item(
        _provider_raw(
            models={
                "seedance-2.0-mini:t2v": "dreamina-seedance-2-0-mini-260615",
            }
        ),
        index=0,
    )

    assert (
        provider.upstream_model_for("seedance-2.0-mini", "t2v")
        == "doubao-seedance-2-0-mini-260615"
    )


def test_parse_third_party_provider_keeps_byteplus_seedance_mini_alias() -> None:
    provider = parse_video_provider_item(
        _provider_raw(
            kind="volcano_third_party",
            base_url="https://www.moyu.info",
            models={
                "seedance-2.0-mini:t2v": "dreamina-seedance-2-0-mini-260615",
            },
        ),
        index=0,
    )

    assert (
        provider.upstream_model_for("seedance-2.0-mini", "t2v")
        == "dreamina-seedance-2-0-mini-260615"
    )


def test_parse_dashscope_happyhorse_provider() -> None:
    provider = parse_video_provider_item(
        _provider_raw(
            name="dashscope-happyhorse",
            kind="dashscope",
            base_url="https://dashscope-intl.aliyuncs.com",
            api_key="dashscope-key",
            models={
                "happyhorse-1.0:t2v": "happyhorse-1.0-t2v",
                "happyhorse-1.0:i2v": "happyhorse-1.0-i2v",
                "happyhorse-1.0:reference": "happyhorse-1.0-r2v",
            },
        ),
        index=0,
    )

    assert provider.kind == "dashscope"
    assert provider.supports("happyhorse-1.0", "t2v")
    assert provider.supports("happyhorse-1.0", "i2v")
    assert provider.supports("happyhorse-1.0", "reference")
    assert (
        provider.upstream_model_for("happyhorse-1.0", "reference")
        == "happyhorse-1.0-r2v"
    )


def test_parse_volcano_third_party_provider() -> None:
    provider = parse_video_provider_item(
        _provider_raw(
            name="moyu",
            kind="volcano_third_party",
            base_url="https://www.moyu.info",
        ),
        index=0,
    )

    assert provider.kind == "volcano_third_party"
    assert provider.base_url == "https://www.moyu.info"
    assert provider.supports("seedance-2.0", "reference")


def test_parse_volcano_newapi_provider() -> None:
    provider = parse_video_provider_item(
        _provider_raw(
            name="volcano-newapi",
            kind="volcano_newapi",
            base_url="https://zz1cc.cc.cd/v1",
            models={
                "video-ds-2.0:t2v": "video-ds-2.0",
                "video-ds-2.0-fast:t2v": "video-ds-2.0-fast",
            },
        ),
        index=0,
    )

    assert provider.kind == "volcano_newapi"
    assert provider.base_url == "https://zz1cc.cc.cd/v1"
    assert provider.supports("video-ds-2.0", "t2v")
    assert provider.supports("video-ds-2.0-fast", "t2v")


def test_parse_omni_flash_provider() -> None:
    provider = parse_video_provider_item(
        _provider_raw(
            name="google-omni-flash",
            kind="omni_flash",
            base_url="https://gateway.example.com/v1",
            models={
                "omni-flash:t2v": "gemini_omni_flash",
                "omni-flash:i2v": "gemini_omni_flash",
                "omni-flash:reference": "gemini_omni_flash",
            },
        ),
        index=0,
    )

    assert provider.kind == "omni_flash"
    assert provider.base_url == "https://gateway.example.com/v1"
    assert provider.supports("omni-flash", "t2v")
    assert provider.supports("omni-flash", "i2v")
    assert provider.supports("omni-flash", "reference")
    assert provider.upstream_model_for("omni-flash", "i2v") == "gemini_omni_flash"


def test_video_provider_config_can_reference_shared_proxy() -> None:
    shared = json.dumps(
        {
            "proxies": [
                {
                    "name": "sg-socks",
                    "type": "socks5",
                    "host": "127.0.0.1",
                    "port": 1080,
                }
            ],
            "providers": [
                {
                    "name": "chat",
                    "base_url": "https://chat.example.com",
                    "api_key": "sk-test",
                }
            ],
        }
    )
    raw = json.dumps({"providers": [_provider_raw(proxy="sg-socks")]})

    providers, proxies, errors = parse_video_provider_config_json(
        raw,
        shared_provider_raw=shared,
    )

    assert errors == []
    assert [proxy.name for proxy in proxies] == ["sg-socks"]
    assert providers[0].proxy_name == "sg-socks"
    assert providers[0].proxy is proxies[0]


def test_video_provider_config_reports_disabled_shared_proxy_reference() -> None:
    shared = json.dumps(
        {
            "proxies": [
                {
                    "name": "sg-socks",
                    "type": "socks5",
                    "host": "127.0.0.1",
                    "port": 1080,
                    "enabled": False,
                }
            ],
            "providers": [
                {
                    "name": "chat",
                    "base_url": "https://chat.example.com",
                    "api_key": "sk-test",
                }
            ],
        }
    )
    raw = json.dumps({"providers": [_provider_raw(proxy="sg-socks")]})

    providers, _proxies, errors = parse_video_provider_config_json(
        raw,
        shared_provider_raw=shared,
    )

    assert providers[0].proxy is None
    assert any("proxy sg-socks is disabled" in error for error in errors)


def test_video_provider_config_reports_disabled_local_proxy_reference() -> None:
    raw = json.dumps(
        {
            "proxies": [
                {
                    "name": "local-socks",
                    "type": "socks5",
                    "host": "127.0.0.1",
                    "port": 1080,
                    "enabled": False,
                }
            ],
            "providers": [_provider_raw(proxy="local-socks")],
        }
    )

    providers, _proxies, errors = parse_video_provider_config_json(
        raw,
        allow_missing_proxy=True,
    )

    assert providers[0].proxy is None
    assert any("proxy local-socks is disabled" in error for error in errors)


def test_video_provider_config_allows_disabled_provider_stale_proxy() -> None:
    raw = json.dumps(
        {
            "proxies": [
                {
                    "name": "local-socks",
                    "type": "socks5",
                    "host": "127.0.0.1",
                    "port": 1080,
                    "enabled": False,
                }
            ],
            "providers": [
                _provider_raw(
                    name="parked",
                    enabled=False,
                    api_key="",
                    proxy="local-socks",
                )
            ],
        }
    )

    providers, _proxies, errors = parse_video_provider_config_json(raw)

    assert errors == []
    assert providers[0].enabled is False
    assert providers[0].proxy is None


def test_video_provider_config_can_defer_missing_proxy_validation() -> None:
    raw = json.dumps({"providers": [_provider_raw(proxy="shared-socks")]})

    providers, _proxies, errors = parse_video_provider_config_json(
        raw,
        allow_missing_proxy=True,
    )

    assert errors == []
    assert providers[0].proxy_name == "shared-socks"
    assert providers[0].proxy is None


def test_select_video_provider_skips_disabled_and_unsupported_entries() -> None:
    raw = json.dumps(
        {
            "providers": [
                _provider_raw(name="disabled", enabled=False),
                _provider_raw(
                    name="i2v-only",
                    models={"seedance-2.0:i2v": "doubao-i2v"},
                ),
            ]
        }
    )
    providers, _proxies, errors = parse_video_provider_config_json(raw)

    assert errors == []
    assert select_video_provider(providers, model="seedance-2.0", action="t2v") is None
    selected = select_video_provider(providers, model="seedance-2.0", action="i2v")
    assert selected is not None
    assert selected.name == "i2v-only"


def test_video_provider_config_reports_missing_required_fields() -> None:
    raw = json.dumps(
        {
            "providers": [
                _provider_raw(name="missing-key", api_key=""),
                _provider_raw(name="missing-models", models={}),
            ]
        }
    )

    providers, _proxies, errors = parse_video_provider_config_json(raw)

    assert providers == []
    assert any("api_key is required" in error for error in errors)
    assert any("models must be a non-empty object" in error for error in errors)
