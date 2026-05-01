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

    from lumen_core.constants import MAX_PROMPT_CHARS
    from lumen_core.schemas import PostMessageIn

    PostMessageIn(idempotency_key="idem", text="x" * MAX_PROMPT_CHARS)

    try:
        PostMessageIn(idempotency_key="idem", text="x" * (MAX_PROMPT_CHARS + 1))
    except ValidationError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected prompt length validation error")
