import json

import pytest

from lumen_core.runtime_settings import get_spec, parse_value, validate_providers


CONTEXT_SETTING_KEYS = {
    "context.compression_enabled",
    "context.compression_trigger_percent",
    "context.summary_target_tokens",
    "context.summary_model",
    "context.summary_min_recent_messages",
    "context.summary_min_interval_seconds",
    "context.summary_input_budget",
    "context.image_caption_enabled",
    "context.image_caption_model",
    "context.compression_circuit_breaker_threshold",
    "context.manual_compact_min_input_tokens",
    "context.manual_compact_cooldown_seconds",
}


def test_context_settings_are_registered():
    for key in CONTEXT_SETTING_KEYS:
        assert get_spec(key) is not None


def test_context_int_setting_bounds_are_enforced():
    spec = get_spec("context.compression_trigger_percent")
    assert spec is not None

    assert parse_value(spec, "80") == 80
    with pytest.raises(ValueError):
        parse_value(spec, "49")
    with pytest.raises(ValueError):
        parse_value(spec, "99")


def test_context_string_setting_parses_as_raw_string():
    spec = get_spec("context.summary_model")
    assert spec is not None

    assert parse_value(spec, "gpt-5.4") == "gpt-5.4"


def test_image_primary_route_setting_is_registered_and_validated():
    spec = get_spec("image.primary_route")
    assert spec is not None

    assert spec.parser is str
    assert spec.env_fallback == "IMAGE_PRIMARY_ROUTE"
    assert parse_value(spec, "responses") == "responses"
    assert parse_value(spec, "image2") == "image2"
    assert parse_value(spec, "image_jobs") == "image_jobs"
    with pytest.raises(ValueError):
        parse_value(spec, "direct")


def test_image_channel_and_engine_settings_are_registered_and_validated():
    channel = get_spec("image.channel")
    engine = get_spec("image.engine")
    assert channel is not None
    assert engine is not None

    assert channel.env_fallback == "IMAGE_CHANNEL"
    assert parse_value(channel, "auto") == "auto"
    assert parse_value(channel, "stream_only") == "stream_only"
    assert parse_value(channel, "image_jobs_only") == "image_jobs_only"
    with pytest.raises(ValueError):
        parse_value(channel, "image_jobs")

    assert engine.env_fallback == "IMAGE_ENGINE"
    assert parse_value(engine, "responses") == "responses"
    assert parse_value(engine, "image2") == "image2"
    assert parse_value(engine, "dual_race") == "dual_race"
    with pytest.raises(ValueError):
        parse_value(engine, "image_jobs")


def test_image_generation_concurrency_setting_is_registered_and_bounded():
    spec = get_spec("image.generation_concurrency")
    assert spec is not None

    assert spec.parser is int
    assert spec.env_fallback == "IMAGE_GENERATION_CONCURRENCY"
    assert spec.min_value == 1
    assert spec.max_value == 32
    assert parse_value(spec, "8") == 8
    with pytest.raises(ValueError):
        parse_value(spec, "0")
    with pytest.raises(ValueError):
        parse_value(spec, "33")


def test_image_output_format_setting_is_registered_and_validated():
    spec = get_spec("image.output_format")
    assert spec is not None

    assert spec.parser is str
    assert spec.env_fallback == "IMAGE_OUTPUT_FORMAT"
    assert parse_value(spec, "jpeg") == "jpeg"
    assert parse_value(spec, "png") == "png"
    with pytest.raises(ValueError):
        parse_value(spec, "webp")


def test_generation_fast_default_setting_is_registered_and_validated():
    spec = get_spec("generation.fast_default")
    assert spec is not None

    assert spec.parser is int
    assert spec.env_fallback == "GENERATION_FAST_DEFAULT"
    assert parse_value(spec, "0") == 0
    assert parse_value(spec, "1") == 1
    with pytest.raises(ValueError):
        parse_value(spec, "2")


def test_ui_nav_visibility_settings_are_registered_and_validated():
    expected_env = {
        "ui.nav.studio_visible": "UI_NAV_STUDIO_VISIBLE",
        "ui.nav.video_visible": "UI_NAV_VIDEO_VISIBLE",
        "ui.nav.projects_visible": "UI_NAV_PROJECTS_VISIBLE",
        "ui.nav.assets_visible": "UI_NAV_ASSETS_VISIBLE",
    }
    for key, env_key in expected_env.items():
        spec = get_spec(key)
        assert spec is not None
        assert spec.parser is int
        assert spec.env_fallback == env_key
        assert parse_value(spec, "0") == 0
        assert parse_value(spec, "1") == 1
        with pytest.raises(ValueError):
            parse_value(spec, "2")


def test_billing_settings_are_registered_and_validated():
    enabled = get_spec("billing.enabled")
    rate = get_spec("billing.usd_to_rmb_rate")
    secret = get_spec("billing.redemption_code_secret")
    threshold = get_spec("billing.low_balance_warn_micro")
    allow_negative = get_spec("billing.allow_negative_balance")
    thresholds = get_spec("billing.image_size_thresholds")
    bootstrap_completed = get_spec("billing.bootstrap_completed")
    show_estimate = get_spec("billing.show_estimate_in_composer")
    assert enabled is not None
    assert rate is not None
    assert secret is not None
    assert threshold is not None
    assert allow_negative is not None
    assert thresholds is not None
    assert bootstrap_completed is not None
    assert show_estimate is not None

    assert enabled.env_fallback == "BILLING_ENABLED"
    assert parse_value(enabled, "0") == 0
    assert parse_value(enabled, "1") == 1
    with pytest.raises(ValueError):
        parse_value(enabled, "true")

    assert parse_value(rate, "1.0") == 1.0
    with pytest.raises(ValueError):
        # rate=0 would silently zero out every chat charge; force admin to use
        # billing.enabled=0 instead.
        parse_value(rate, "0")

    assert secret.sensitive is True
    assert parse_value(secret, "a" * 16) == "a" * 16
    with pytest.raises(ValueError):
        parse_value(secret, "")
    with pytest.raises(ValueError):
        parse_value(secret, "tooshort")

    assert parse_value(threshold, "2000000") == 2_000_000

    assert parse_value(allow_negative, "0") == 0
    assert parse_value(allow_negative, "1") == 1
    with pytest.raises(ValueError):
        parse_value(allow_negative, "true")

    assert parse_value(bootstrap_completed, "0") == 0
    assert parse_value(bootstrap_completed, "1") == 1
    with pytest.raises(ValueError):
        parse_value(bootstrap_completed, "true")

    assert parse_value(show_estimate, "0") == 0
    assert parse_value(show_estimate, "1") == 1
    with pytest.raises(ValueError):
        parse_value(show_estimate, "true")

    valid_thresholds = '{"1k": 1572864, "2k": 3686400}'
    assert parse_value(thresholds, valid_thresholds) == valid_thresholds
    with pytest.raises(ValueError):
        parse_value(thresholds, "")
    with pytest.raises(ValueError):
        parse_value(thresholds, "not-json")
    with pytest.raises(ValueError):
        parse_value(thresholds, "[]")
    with pytest.raises(ValueError):
        parse_value(thresholds, '{"1k": -1}')
    with pytest.raises(ValueError):
        parse_value(thresholds, '{"1k": 1.5}')


def test_image_job_base_url_setting_is_registered_and_validated():
    spec = get_spec("image.job_base_url")
    assert spec is not None

    assert spec.env_fallback == "IMAGE_JOB_BASE_URL"
    assert (
        parse_value(spec, "https://image-job.example.com/")
        == "https://image-job.example.com"
    )
    assert parse_value(spec, "http://localhost:8080/v1") == "http://localhost:8080/v1"
    with pytest.raises(ValueError):
        parse_value(spec, "image-job.example.com")
    with pytest.raises(ValueError):
        parse_value(spec, "https://user:pass@image-job.example")


def test_site_public_base_url_setting_is_registered_and_validated():
    spec = get_spec("site.public_base_url")
    assert spec is not None

    assert spec.env_fallback == "PUBLIC_BASE_URL"
    assert (
        parse_value(spec, "https://lumen.example.com/") == "https://lumen.example.com"
    )
    assert parse_value(spec, "http://localhost:3000") == "http://localhost:3000"
    with pytest.raises(ValueError):
        parse_value(spec, "lumen.example.com")
    with pytest.raises(ValueError):
        parse_value(spec, "https://lumen.example.com/api")
    with pytest.raises(ValueError):
        parse_value(spec, "https://user:pass@lumen.example.com")


def test_share_expiration_days_setting_is_registered_and_bounded():
    spec = get_spec("site.share_expiration_days")
    assert spec is not None

    assert spec.parser is int
    assert spec.env_fallback == "SHARE_EXPIRATION_DAYS"
    assert parse_value(spec, "0") == 0
    assert parse_value(spec, "30") == 30
    with pytest.raises(ValueError):
        parse_value(spec, "-1")
    with pytest.raises(ValueError):
        parse_value(spec, "3651")


def test_update_settings_are_registered_and_validated():
    use_proxy = get_spec("update.use_proxy_pool")
    proxy_name = get_spec("update.proxy_name")
    assert use_proxy is not None
    assert proxy_name is not None

    assert use_proxy.parser is int
    assert use_proxy.env_fallback == "LUMEN_UPDATE_USE_PROXY_POOL"
    assert parse_value(use_proxy, "0") == 0
    assert parse_value(use_proxy, "1") == 1
    with pytest.raises(ValueError):
        parse_value(use_proxy, "2")

    assert proxy_name.parser is str
    assert proxy_name.env_fallback == "LUMEN_UPDATE_PROXY_NAME"
    assert parse_value(proxy_name, "s5-us") == "s5-us"


def test_model_library_sync_proxy_settings_are_registered_and_validated():
    use_proxy = get_spec("model_library.sync_use_proxy_pool")
    proxy_name = get_spec("model_library.sync_proxy_name")
    assert use_proxy is not None
    assert proxy_name is not None

    assert use_proxy.parser is int
    assert use_proxy.env_fallback == "APPAREL_MODEL_LIBRARY_SYNC_USE_PROXY_POOL"
    assert parse_value(use_proxy, "0") == 0
    assert parse_value(use_proxy, "1") == 1
    with pytest.raises(ValueError):
        parse_value(use_proxy, "2")

    assert proxy_name.parser is str
    assert proxy_name.env_fallback == "APPAREL_MODEL_LIBRARY_SYNC_PROXY_NAME"
    assert parse_value(proxy_name, "s5-us") == "s5-us"


def test_video_providers_setting_allows_shared_proxy_reference() -> None:
    spec = get_spec("video.providers")
    assert spec is not None
    raw = json.dumps(
        {
            "providers": [
                {
                    "name": "video-main",
                    "kind": "volcano",
                    "base_url": "https://ark.example.com/api/v3",
                    "api_key": "sk-test",
                    "proxy": "shared-socks",
                    "models": {"seedance-2.0:t2v": "seedance-upstream"},
                }
            ]
        }
    )

    assert parse_value(spec, raw) == raw


def test_legacy_text_to_image_route_key_still_registered_for_fallback():
    """旧键保留在 SUPPORTED_SETTINGS，让现有 DB 行仍能被 worker resolve 拿到。"""
    spec = get_spec("image.text_to_image_primary_route")
    assert spec is not None
    assert spec.parser is str
    # 描述里应明确标记 deprecated，便于 grep
    assert "deprecated" in spec.description.lower() or "DEPRECATED" in spec.description
    assert parse_value(spec, "image2") == "image2"


def test_upstream_timeout_settings_are_registered_and_bounded():
    spec = get_spec("upstream.read_timeout_s")
    assert spec is not None
    assert spec.parser is float
    assert spec.env_fallback == "UPSTREAM_READ_TIMEOUT_S"
    assert parse_value(spec, "180.5") == 180.5
    with pytest.raises(ValueError):
        parse_value(spec, "0")


def test_validate_providers_accepts_http_and_https_provider_array():
    raw = json.dumps(
        [
            {
                "name": "primary",
                "base_url": "https://upstream.example/v1",
                "api_key": "sk-test",
            },
            {
                "name": "internal",
                "base_url": "http://10.0.0.8:8000/v1",
                "api_key": "sk-internal",
            },
        ]
    )

    assert validate_providers(f" {raw} ") == raw


def test_validate_providers_accepts_proxy_config_object():
    raw = json.dumps(
        {
            "proxies": [
                {
                    "name": "egress",
                    "type": "s5",
                    "host": "127.0.0.1",
                    "port": 1080,
                },
                {
                    "name": "ssh-hop",
                    "type": "ssh",
                    "host": "ssh.example.com",
                    "port": 22,
                    "username": "ubuntu",
                    "private_key_path": "/home/lumen/.ssh/id_ed25519",
                },
            ],
            "providers": [
                {
                    "name": "primary",
                    "base_url": "https://upstream.example/v1",
                    "api_key": "sk-test",
                    "proxy": "egress",
                }
            ],
        }
    )

    assert validate_providers(raw) == raw


def test_validate_providers_rejects_unknown_proxy_reference():
    raw = json.dumps(
        {
            "proxies": [],
            "providers": [
                {
                    "name": "primary",
                    "base_url": "https://upstream.example/v1",
                    "api_key": "sk-test",
                    "proxy": "missing",
                }
            ],
        }
    )

    with pytest.raises(ValueError, match="references unknown proxy"):
        validate_providers(raw)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "must not be empty"),
        ("[", "not valid JSON"),
        ("[]", "non-empty JSON array"),
        (json.dumps(["not-object"]), "must be an object"),
        (json.dumps([{"api_key": "sk-test"}]), "base_url is required"),
        (
            json.dumps([{"base_url": "https://upstream.example"}]),
            "api_key is required",
        ),
        (
            json.dumps([{"base_url": "https:///v1", "api_key": "sk"}]),
            "must include a hostname",
        ),
        (
            json.dumps([{"base_url": "https://u:p@upstream.example", "api_key": "sk"}]),
            "must not include credentials",
        ),
    ],
)
def test_validate_providers_rejects_invalid_provider_config(raw, message):
    with pytest.raises(ValueError, match=message):
        validate_providers(raw)


def test_validate_providers_reports_missing_url_scheme():
    with pytest.raises(ValueError, match="has no scheme"):
        validate_providers(
            '[{"name":"bad","base_url":"not-a-url","api_key":"sk-test"}]'
        )


def test_validate_providers_reports_unsupported_scheme():
    with pytest.raises(ValueError, match="must use http or https"):
        validate_providers(
            '[{"name":"bad","base_url":"ftp://example.com","api_key":"sk-test"}]'
        )
