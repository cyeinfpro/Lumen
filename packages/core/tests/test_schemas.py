def test_provider_stats_schemas_are_exported():
    namespace: dict[str, object] = {}
    exec("from lumen_core.schemas import *", namespace)

    assert "ProviderStatsItem" in namespace
    assert "ProviderStatsOut" in namespace


def test_video_provider_schema_accepts_dashscope_happyhorse():
    from lumen_core.schemas import VideoProvidersUpdateIn

    body = VideoProvidersUpdateIn(
        enabled=True,
        items=[
            {
                "name": "dashscope-happyhorse",
                "kind": "dashscope",
                "base_url": "https://dashscope-intl.aliyuncs.com",
                "api_key": "sk-test",
                "enabled": True,
                "priority": 100,
                "weight": 1,
                "concurrency": 2,
                "models": {
                    "happyhorse-1.0:t2v": "happyhorse-1.0-t2v",
                    "happyhorse-1.0:i2v": "happyhorse-1.0-i2v",
                    "happyhorse-1.0:reference": "happyhorse-1.0-r2v",
                },
            }
        ],
    )

    item = body.items[0]
    assert item.kind == "dashscope"
    assert item.models["happyhorse-1.0:t2v"] == "happyhorse-1.0-t2v"
    assert item.models["happyhorse-1.0:i2v"] == "happyhorse-1.0-i2v"
    assert item.models["happyhorse-1.0:reference"] == "happyhorse-1.0-r2v"


def test_video_provider_schema_accepts_volcano_third_party():
    from lumen_core.schemas import VideoProvidersUpdateIn

    body = VideoProvidersUpdateIn(
        enabled=True,
        items=[
            {
                "name": "moyu",
                "kind": "volcano_third_party",
                "base_url": "https://www.moyu.info",
                "api_key": "sk-test",
                "enabled": True,
                "priority": 100,
                "weight": 1,
                "concurrency": 10,
                "models": {
                    "seedance-2.0-fast:reference": "doubao-seedance-2-0-fast-260128",
                },
            }
        ],
    )

    item = body.items[0]
    assert item.kind == "volcano_third_party"
    assert (
        item.models["seedance-2.0-fast:reference"] == "doubao-seedance-2-0-fast-260128"
    )


def test_video_provider_schema_accepts_volcano_newapi():
    from lumen_core.schemas import VideoProvidersUpdateIn

    body = VideoProvidersUpdateIn(
        enabled=True,
        items=[
            {
                "name": "volcano-newapi",
                "kind": "volcano_newapi",
                "base_url": "https://zz1cc.cc.cd",
                "api_key": "sk-test",
                "enabled": True,
                "priority": 100,
                "weight": 1,
                "concurrency": 10,
                "models": {
                    "video-ds-2.0:t2v": "video-ds-2.0",
                    "video-ds-2.0-fast:t2v": "video-ds-2.0-fast",
                },
            }
        ],
    )

    item = body.items[0]
    assert item.kind == "volcano_newapi"
    assert item.models["video-ds-2.0:t2v"] == "video-ds-2.0"
    assert item.models["video-ds-2.0-fast:t2v"] == "video-ds-2.0-fast"


def test_video_provider_schema_accepts_omni_flash():
    from lumen_core.schemas import VideoProvidersUpdateIn

    body = VideoProvidersUpdateIn(
        enabled=True,
        items=[
            {
                "name": "google-omni-flash",
                "kind": "omni_flash",
                "base_url": "https://gateway.example.com",
                "api_key": "sk-test",
                "enabled": True,
                "priority": 90,
                "weight": 1,
                "concurrency": 2,
                "models": {
                    "omni-flash:t2v": "gemini_omni_flash",
                    "omni-flash:i2v": "gemini_omni_flash",
                    "omni-flash:reference": "gemini_omni_flash",
                },
            }
        ],
    )

    item = body.items[0]
    assert item.kind == "omni_flash"
    assert item.models["omni-flash:t2v"] == "gemini_omni_flash"


def test_image_params_support_render_and_output_options():
    from pydantic import ValidationError

    from lumen_core.schemas import ImageParamsIn

    params = ImageParamsIn(
        render_quality="medium",
        output_format="webp",
        output_compression=88,
        background="transparent",
        moderation="low",
    )

    assert params.render_quality == "medium"
    assert params.output_format == "png"
    assert params.output_compression is None
    assert params.background == "transparent"
    assert params.moderation == "low"
    assert ImageParamsIn().quality == "4k"
    assert ImageParamsIn().aspect_ratio == "7:10"
    assert ImageParamsIn().render_quality == "high"
    assert ImageParamsIn().count == 1
    assert ImageParamsIn(count=10).count == 10
    assert ImageParamsIn().output_format is None
    assert ImageParamsIn().fast is None

    for kwargs in (
        {"background": "checkerboard"},
        {"count": 11},
        {"output_compression": 101},
    ):
        try:
            ImageParamsIn(**kwargs)
        except ValidationError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected validation error for {kwargs}")


def test_post_message_prompt_limit_uses_shared_constant():
    from pydantic import ValidationError

    from lumen_core.constants import MAX_MESSAGE_ATTACHMENTS, MAX_PROMPT_CHARS
    from lumen_core.schemas import PostMessageIn

    assert MAX_MESSAGE_ATTACHMENTS == 16
    PostMessageIn(idempotency_key="idem", text="x" * MAX_PROMPT_CHARS)
    PostMessageIn(
        idempotency_key="idem",
        text="ok",
        attachment_image_ids=[f"img-{i}" for i in range(MAX_MESSAGE_ATTACHMENTS)],
    )

    try:
        PostMessageIn(idempotency_key="idem", text="x" * (MAX_PROMPT_CHARS + 1))
    except ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected prompt length validation error")

    try:
        PostMessageIn(
            idempotency_key="idem",
            text="ok",
            attachment_image_ids=[
                f"img-{i}" for i in range(MAX_MESSAGE_ATTACHMENTS + 1)
            ],
        )
    except ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected attachment count validation error")


def test_post_message_structured_attachment_contract():
    from pydantic import ValidationError

    from lumen_core.schemas import PostMessageIn

    body = PostMessageIn(
        idempotency_key="idem",
        text="ok",
        attachments=[
            {
                "image_id": "img-product",
                "role": "product",
                "label": "商品图",
                "weight": 0.7,
            }
        ],
        source="chat",
        action_source="revise",
        trace_id="trace-ui-1",
    )

    assert body.attachment_image_ids == ["img-product"]
    assert body.input_images == ["img-product"]
    assert body.attachments[0].role == "product"
    assert body.attachments[0].label == "商品图"
    assert body.attachments[0].weight == 0.7
    assert body.source == "chat"
    assert body.action_source == "revise"
    assert body.trace_id == "trace-ui-1"

    try:
        PostMessageIn(
            idempotency_key="idem",
            text="ok",
            attachment_image_ids=["img-a"],
            attachments=[{"image_id": "img-b", "role": "style"}],
        )
    except ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected attachment contract validation error")

    try:
        PostMessageIn(
            idempotency_key="idem",
            text="ok",
            attachment_image_ids=["img-a", "img-b"],
            attachments=[
                {"image_id": "img-b", "role": "style"},
                {"image_id": "img-a", "role": "product"},
            ],
        )
    except ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected order-sensitive attachment validation error")


def test_model_library_generate_accepts_multiple_genders():
    from lumen_core.schemas import ApparelModelLibraryGenerateIn

    body = ApparelModelLibraryGenerateIn(
        age_segment="young_adult",
        genders=["female", "male"],
        count=4,
    )

    assert body.gender is None
    assert body.genders == ["female", "male"]


def test_showcase_images_accepts_landscape_aspect_ratios():
    from lumen_core.schemas import ShowcaseImagesCreateIn

    for aspect in ("4:3", "3:2", "16:9", "21:9"):
        body = ShowcaseImagesCreateIn(aspect_ratio=aspect)
        assert body.aspect_ratio == aspect


def test_showcase_images_defaults_to_gpt55_preflight_scene_planner():
    from lumen_core.schemas import ShowcaseImagesCreateIn

    body = ShowcaseImagesCreateIn()

    assert body.scene_planner == "gpt55_preflight"
    assert body.scene_strategy == "natural_series"
    assert body.scene_variety == "rich"
    assert body.continuity_anchor == "accessory"
    assert body.allow_pet is False
    assert body.allow_background_people is True
    assert body.shot_plan == [
        "front_full_body",
        "natural_pose",
        "detail_half_body",
    ]


def test_video_create_schema_enforces_action_image_contract():
    from pydantic import ValidationError

    from lumen_core.schemas import VideoCreateIn

    VideoCreateIn(
        action="t2v",
        model="seedance-2.0",
        prompt="make a clip",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="idem-t2v",
    )
    VideoCreateIn(
        action="i2v",
        model="seedance-2.0",
        prompt="animate this",
        input_image_id="img-1",
        duration_s=5,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="idem-i2v",
    )
    VideoCreateIn(
        action="reference",
        model="seedance-2.0",
        prompt="[Image 1] and [Video 1] keep the character consistent",
        reference_media=[
            {
                "kind": "image",
                "image_id": "img-1",
                "label": "Image 1",
                "ref_id": "ref:image:1",
            },
            {
                "kind": "video",
                "video_id": "vid-1",
                "label": "Video 1",
                "ref_id": "ref:video:1",
            },
        ],
        duration_s=5,
        resolution="480p",
        aspect_ratio="adaptive",
        idempotency_key="idem-reference",
    )
    VideoCreateIn(
        action="t2v",
        model="seedance-2.0",
        prompt="make a clip with smart duration",
        duration_s=-1,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="idem-t2v-smart-duration",
    )
    VideoCreateIn(
        action="t2v",
        model="happyhorse-1.0",
        prompt="make a short clip",
        duration_s=3,
        resolution="720p",
        aspect_ratio="16:9",
        idempotency_key="idem-t2v-three-seconds",
    )
    VideoCreateIn(
        action="t2v",
        model="omni-flash",
        prompt="make a high resolution clip",
        duration_s=6,
        resolution="4k",
        aspect_ratio="16:9",
        idempotency_key="idem-t2v-4k",
    )

    for kwargs in (
        {"action": "t2v", "input_image_id": "img-1"},
        {"action": "t2v", "reference_media": [{"kind": "image", "image_id": "img-1"}]},
        {"action": "i2v", "input_image_id": None},
        {
            "action": "i2v",
            "input_image_id": "img-1",
            "reference_media": [{"kind": "image", "image_id": "img-2"}],
        },
        {"action": "reference", "reference_media": []},
        {"action": "t2v", "duration_s": 2},
        {"action": "t2v", "aspect_ratio": "4:5"},
        {"action": "t2v", "fps": 24},
        {
            "action": "reference",
            "input_image_id": "img-1",
            "reference_media": [{"kind": "image", "image_id": "img-2"}],
        },
        {
            "action": "reference",
            "reference_media": [
                {
                    "kind": "image",
                    "image_id": "img-2",
                    "url": "https://example.com/a.png",
                }
            ],
        },
        {
            "action": "reference",
            "reference_media": [{"kind": "image", "video_id": "vid-1"}],
        },
        {
            "action": "reference",
            "reference_media": [
                {
                    "kind": "image",
                    "image_id": "img-2",
                    "ref_id": "ref:video:1",
                }
            ],
        },
        {
            "action": "reference",
            "reference_media": [
                {
                    "kind": "image",
                    "image_id": "img-2",
                    "ref_id": "image-1",
                }
            ],
        },
        {
            "action": "reference",
            "reference_media": [
                {"kind": "image", "image_id": f"img-{idx}"} for idx in range(10)
            ],
        },
        {
            "action": "reference",
            "reference_media": [
                {"kind": "video", "video_id": f"vid-{idx}"} for idx in range(4)
            ],
        },
        {
            "action": "reference",
            "reference_media": [
                {
                    "kind": "audio",
                    "url": f"https://example.com/ref-{idx}.mp3",
                }
                for idx in range(2)
            ],
        },
    ):
        payload = {
            "model": "seedance-2.0",
            "prompt": "make a clip",
            "duration_s": 5,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "idempotency_key": "idem-bad",
            **kwargs,
        }
        try:
            VideoCreateIn(**payload)
        except ValidationError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected validation error for {kwargs}")


def test_video_reference_audio_media_is_url_only():
    from pydantic import ValidationError

    from lumen_core.schemas import VideoCreateIn, VideoReferenceMediaIn

    audio = VideoReferenceMediaIn(
        kind="audio",
        url="https://cdn.example.com/ref.mp3",
        label="Audio 1",
        ref_id=" REF:AUDIO:1 ",
    )

    assert audio.ref_id == "ref:audio:1"
    VideoCreateIn(
        action="reference",
        model="video-ds-2.0-fast",
        prompt="use the reference image and audio",
        reference_media=[
            {"kind": "image", "url": "https://cdn.example.com/ref.png"},
            audio,
        ],
        duration_s=15,
        resolution="720p",
        aspect_ratio="9:16",
        idempotency_key="idem-reference-audio",
    )

    for kwargs in (
        {"kind": "audio", "image_id": "img-1"},
        {"kind": "audio", "video_id": "vid-1"},
        {
            "kind": "audio",
            "url": "https://cdn.example.com/ref.mp3",
            "ref_id": "ref:image:1",
        },
    ):
        try:
            VideoReferenceMediaIn(**kwargs)
        except ValidationError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected validation error for {kwargs}")


def test_video_reference_media_rejects_unsafe_url_sources():
    from pydantic import ValidationError

    from lumen_core.schemas import VideoReferenceMediaIn

    VideoReferenceMediaIn(kind="image", url="https://cdn.example.com/ref.png")
    ref = VideoReferenceMediaIn(
        kind="image", url="https://cdn.example.com/ref.png", ref_id=" REF:IMAGE:1 "
    )
    assert ref.ref_id == "ref:image:1"
    VideoReferenceMediaIn(kind="image", url="asset://asset-20260609161523-stlqd")
    mixed_asset = VideoReferenceMediaIn(
        kind="image",
        url=" `Asset : //ASSET-20260609161523-STLQD` ",
    )
    assert mixed_asset.url == "asset://asset-20260609161523-stlqd"

    for url in (
        "file:///etc/passwd",
        "asset://",
        "http://cdn.example.com/ref.png",
        "http://169.254.169.254/latest/meta-data",
        "https://127.0.0.1/ref.png",
        "https://[::ffff:127.0.0.1]/ref.png",
        "https://0177.0.0.1/ref.png",
        "https://localhost/ref.png",
        "https://user:pass@example.com/ref.png",
    ):
        try:
            VideoReferenceMediaIn(kind="image", url=url)
        except ValidationError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"expected validation error for {url}")
