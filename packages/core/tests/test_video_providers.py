from __future__ import annotations

import json
from dataclasses import replace

import pytest

from lumen_core.providers import ProviderProxyDefinition
from lumen_core.video_providers import (
    VideoProviderDefinition,
    parse_video_provider_config_json,
    parse_video_provider_item,
    seedance_20_variant,
    select_video_provider,
    video_provider_binding_fingerprint,
    video_reference_media_limits,
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


def test_video_provider_definition_preserves_legacy_positional_order() -> None:
    proxy = ProviderProxyDefinition(
        "proxy-1",
        "socks5",
        "127.0.0.1",
        1080,
    )
    provider = VideoProviderDefinition(
        "legacy",
        "fake",
        "https://video.example.com",
        "api-key",
        False,
        7,
        8,
        9,
        True,
        {"model:t2v": "upstream-model"},
        "proxy-1",
        proxy,
    )

    assert provider.enabled is False
    assert provider.priority == 7
    assert provider.weight == 8
    assert provider.concurrency == 9
    assert provider.supports_idempotency is True
    assert provider.models == {"model:t2v": "upstream-model"}
    assert provider.proxy_name == "proxy-1"
    assert provider.proxy is proxy
    assert provider.access_key_id == ""
    assert provider.secret_access_key == ""
    assert provider.project_name == "default"
    assert provider.region == "cn-beijing"


@pytest.mark.parametrize(
    ("identifier", "expected"),
    [
        ("seedance-2.0", "standard"),
        ("doubao-seedance-2-0-260128", "standard"),
        ("video-ds-2.0-fast", "fast"),
        ("dreamina-seedance-2-0-mini-260615", "mini"),
        ("namespace/doubao-seedance-2-0-fast-260128", "fast"),
    ],
)
def test_seedance_20_variant_matches_supported_tokens(
    identifier: str,
    expected: str,
) -> None:
    assert seedance_20_variant(identifier) == expected


@pytest.mark.parametrize(
    "identifier",
    [
        "not-seedance-2.0",
        "not-video-ds-2.0",
        "prefix-seedance-2.0-fast",
        "seedance-2.0-faster",
        "video-ds-2.0-miniature",
    ],
)
def test_seedance_20_variant_rejects_substring_false_positives(
    identifier: str,
) -> None:
    assert seedance_20_variant(identifier) is None


def test_video_provider_binding_fingerprint_is_stable_and_secret_safe() -> None:
    proxy = ProviderProxyDefinition(
        name="proxy-1",
        protocol="socks5",
        host="127.0.0.1",
        port=1080,
        username="proxy-user",
        password="proxy-secret",
    )
    provider = replace(
        parse_video_provider_item(
            _provider_raw(
                access_key_id="AKLTasset",
                secret_access_key="secret-asset-key",
            ),
            index=0,
        ),
        proxy_name=proxy.name,
        proxy=proxy,
    )

    fingerprint = video_provider_binding_fingerprint(provider)

    assert len(fingerprint) == 64
    assert fingerprint == video_provider_binding_fingerprint(provider)
    assert "ark-key" not in fingerprint
    assert "AKLTasset" not in fingerprint
    assert "secret-asset-key" not in fingerprint
    assert "proxy-secret" not in fingerprint
    assert fingerprint != video_provider_binding_fingerprint(
        replace(provider, secret_access_key="rotated-secret")
    )
    assert fingerprint != video_provider_binding_fingerprint(
        replace(provider, models={"seedance-2.0:t2v": "different-model"})
    )
    assert fingerprint != video_provider_binding_fingerprint(
        replace(provider, proxy=replace(proxy, password="rotated-proxy-secret"))
    )
    assert fingerprint == video_provider_binding_fingerprint(
        replace(provider, priority=999, weight=999, concurrency=32)
    )


def test_parse_volcano_asset_credentials_and_defaults() -> None:
    provider = parse_video_provider_item(
        _provider_raw(
            access_key_id="AKLTasset",
            secret_access_key="secret-asset-key",
        ),
        index=0,
    )

    assert provider.access_key_id == "AKLTasset"
    assert provider.secret_access_key == "secret-asset-key"
    assert provider.project_name == "default"
    assert provider.region == "cn-beijing"
    assert provider.asset_management_ready is True
    assert "secret-asset-key" not in repr(provider)
    assert "AKLTasset" not in repr(provider)


def test_non_volcano_provider_ignores_asset_credentials() -> None:
    provider = parse_video_provider_item(
        _provider_raw(
            kind="dashscope",
            access_key_id="AKLTasset",
            secret_access_key="secret-asset-key",
            project_name="should-not-persist",
            region="cn-shanghai",
        ),
        index=0,
    )

    assert provider.access_key_id == ""
    assert provider.secret_access_key == ""
    assert provider.asset_management_ready is False


@pytest.mark.parametrize(
    "region",
    [
        "cn.beijing",
        "cn/beijing",
        "cn@beijing",
        "CN-beijing",
        "cn beijing",
        "-cn-beijing",
        "cn-beijing-",
    ],
)
def test_parse_volcano_provider_rejects_unsafe_region(region: str) -> None:
    raw = json.dumps({"providers": [_provider_raw(region=region)]})

    providers, _proxies, errors = parse_video_provider_config_json(raw)

    assert providers == []
    assert any("region must use lowercase letters" in error for error in errors)


def test_reference_media_limits_match_provider_adapters() -> None:
    assert video_reference_media_limits("volcano") == {
        "image": 9,
        "video": 3,
        "audio": 3,
    }
    assert video_reference_media_limits("volcano_newapi") == {
        "image": 4,
        "video": 3,
        "audio": 1,
    }
    assert video_reference_media_limits("dashscope") == {"image": 9}
    assert video_reference_media_limits("omni_flash") == {"image": 9}
    assert video_reference_media_limits("veo") == {}


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
