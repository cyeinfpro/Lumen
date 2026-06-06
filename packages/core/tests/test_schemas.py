def test_provider_stats_schemas_are_exported():
    namespace: dict[str, object] = {}
    exec("from lumen_core.schemas import *", namespace)

    assert "ProviderStatsItem" in namespace
    assert "ProviderStatsOut" in namespace


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
    assert ImageParamsIn().render_quality == "medium"
    assert ImageParamsIn().output_format is None
    assert ImageParamsIn().fast is None

    for kwargs in (
        {"background": "checkerboard"},
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
            {"kind": "image", "image_id": "img-1", "label": "Image 1"},
            {"kind": "video", "video_id": "vid-1", "label": "Video 1"},
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
        {"action": "t2v", "duration_s": 3},
        {"action": "t2v", "resolution": "4k"},
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
                {"kind": "image", "image_id": f"img-{idx}"} for idx in range(10)
            ],
        },
        {
            "action": "reference",
            "reference_media": [
                {"kind": "video", "video_id": f"vid-{idx}"} for idx in range(4)
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
