from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app.canvas_services import plan_pricing
from app.public_urls import resolve_configured_public_base_url
from app.services.video import reference_media
from app.services.video.submission_preflight import validate_provider_submission


class SettingsSession:
    def __init__(self, value):
        self.value = value

    async def execute(self, statement):
        assert "system_settings" in str(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: self.value)


@pytest.mark.asyncio
async def test_background_media_uses_only_trusted_configured_public_base(monkeypatch):
    monkeypatch.setattr(
        "app.public_urls.settings",
        SimpleNamespace(public_base_url="https://media.example.test"),
    )
    assert (
        await resolve_configured_public_base_url(SettingsSession(None))
        == "https://media.example.test"
    )
    assert (
        await resolve_configured_public_base_url(
            SettingsSession("https://override.example.test")
        )
        == "https://override.example.test"
    )
    body = SimpleNamespace(action="i2v", input_image_id="local", reference_media=())
    assert (
        await reference_media.reference_public_base_url(
            None,
            SettingsSession(None),
            body,
            None,
            requires_public_media=True,
        )
        == "https://media.example.test"
    )


@pytest.mark.asyncio
async def test_background_required_media_rejects_missing_config_but_inline_survives(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.public_urls.settings", SimpleNamespace(public_base_url=None)
    )
    body = SimpleNamespace(action="i2v", input_image_id="local", reference_media=())
    with pytest.raises(HTTPException):
        await reference_media.reference_public_base_url(
            None, SettingsSession(None), body, None, requires_public_media=True
        )
    assert (
        await reference_media.reference_public_base_url(
            None, SettingsSession(None), body, None, prefers_public_media_url=True
        )
        is None
    )


@pytest.mark.parametrize(
    "provider,aspect,media,code",
    [
        (
            "volcano_newapi",
            "16:9",
            [{"kind": "image"}] * 5,
            "too_many_reference_images",
        ),
        ("omni_flash", "4:3", [], "invalid_aspect_ratio"),
        ("dashscope", "16:9", [{"kind": "video"}], "unsupported_reference_media"),
        (
            "volcano_newapi",
            "16:9",
            [{"kind": "video", "upstream_reference_duration_ms": 16000}],
            "invalid_reference_video_duration",
        ),
        (
            "volcano_newapi",
            "16:9",
            [{"kind": "video", "upstream_reference_duration_ms": 8000}] * 2,
            "reference_video_duration_total_exceeded",
        ),
    ],
)
def test_preview_and_submission_share_static_provider_rejections(
    provider, aspect, media, code
):
    provider = SimpleNamespace(kind=provider, upstream_model_for=lambda *_: "model")
    body = SimpleNamespace(model="model", action="reference", aspect_ratio=aspect)
    with pytest.raises(HTTPException) as denied:
        validate_provider_submission(provider, body, media)
    assert denied.value.detail["error"]["code"] == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_kind,count,aspect,code",
    [
        ("volcano_newapi", 5, "16:9", "too_many_reference_images"),
        ("omni_flash", 1, "4:3", "invalid_aspect_ratio"),
    ],
)
async def test_real_plan_quote_rejects_before_cost_or_task_admission(
    monkeypatch, provider_kind, count, aspect, code
):
    provider = SimpleNamespace(
        kind=provider_kind, upstream_model_for=lambda *_: "model"
    )

    async def ready(*_):
        return provider, {}

    async def options(*_):
        return SimpleNamespace(enabled=True, models=[SimpleNamespace(model="model")])

    async def forbidden(*_args, **_kwargs):
        pytest.fail("invalid static input reached cost admission")

    monkeypatch.setattr(plan_pricing, "require_video_create_ready", ready)
    monkeypatch.setattr(plan_pricing, "get_video_options", options)
    monkeypatch.setattr(plan_pricing, "estimate_video_cost", forbidden)
    nodes = [
        {"id": str(index), "type": "image_generate", "config": {}}
        for index in range(count)
    ]
    node = {
        "id": "target",
        "type": "video_reference_generate",
        "config": {"model": "model", "mode": "reference", "aspect_ratio": aspect},
    }
    graph = {
        "nodes": [*nodes, node],
        "edges": [
            {
                "id": "e" + source["id"],
                "source_node_id": source["id"],
                "target_node_id": "target",
                "target_handle": "reference_images",
            }
            for source in nodes
        ],
    }
    with pytest.raises(HTTPException) as denied:
        await plan_pricing.quote_video_node(
            object(), user=SimpleNamespace(id="u"), graph=graph, node=node
        )
    assert denied.value.detail["error"]["code"] == code
