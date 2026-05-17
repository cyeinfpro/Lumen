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
