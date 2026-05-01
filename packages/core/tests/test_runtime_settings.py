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


def test_image_output_format_setting_is_registered_and_validated():
    spec = get_spec("image.output_format")
    assert spec is not None

    assert spec.parser is str
    assert spec.env_fallback == "IMAGE_OUTPUT_FORMAT"
    assert parse_value(spec, "jpeg") == "jpeg"
    assert parse_value(spec, "png") == "png"
    with pytest.raises(ValueError):
        parse_value(spec, "webp")


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
    assert parse_value(spec, "https://lumen.example.com/") == "https://lumen.example.com"
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
            json.dumps(
                [{"base_url": "https://u:p@upstream.example", "api_key": "sk"}]
            ),
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
