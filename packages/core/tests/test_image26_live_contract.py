"""Offline contracts from the dated report, not live entitlement assertions."""
import pytest
from lumen_core.svg_options import svg_author_recommendation, public_svg_usage
from lumen_core.schema_models.messaging import PostMessageIn

@pytest.mark.parametrize("url", ["https://flux.infpro.me/v1", "https://flux.infpro.me/v1/", "https://flux.infpro.me:443/v1"])
def test_recommendation_is_scoped_readonly_evidence(url):
    assert svg_author_recommendation(url) == {
        "model": "gpt-6.1-sol", "tested_at": "2026-11-05",
        "provider_base_url": "https://flux.infpro.me/v1",
        "access_guaranteed": False, "selection_required": True}

@pytest.mark.parametrize("url", [None, "", "https://example.com/v1", "https://flux.infpro.me.evil/v1",
    "http://flux.infpro.me/v1", "https://flux.infpro.me/v2", "https://flux.infpro.me/v1/extra",
    "https://flux.infpro.me:444/v1", "https://secret@flux.infpro.me/v1",
    "https://flux.infpro.me/v1?token=secret", "https://flux.infpro.me/v1#x"])
def test_unknown_provider_has_no_inferred_author(url):
    assert svg_author_recommendation(url) is None

@pytest.mark.parametrize("author", [None, {"model": "explicit-author"}])
def test_native_svg_edit_is_rejected_at_api_schema(author):
    with pytest.raises(ValueError, match="Native SVG editing is unsupported"):
        PostMessageIn(idempotency_key="offline", text="redraw", intent="image_to_image",
            image_params={"model": "gpt-image-2.6-flare", "output_format": "svg", "svg": author})

def test_author_usage_is_separate_allowlisted_and_has_no_invented_rate():
    usage = {"model": "gpt-6.1-sol", "input_tokens": 1572, "output_tokens": 3071,
        "total_tokens": 4643, "output_tokens_details": {"reasoning_tokens": 0},
        "api_key": "not-retained", "cost": 99}
    normalized = public_svg_usage(usage)
    assert normalized == {k:v for k,v in usage.items() if k not in {"api_key", "cost"}}
    assert public_svg_usage({"input_tokens": True, "output_tokens": -1, "total_tokens": "2"}) is None


def test_real_svg_fixtures_and_gif_retiming():
    import hashlib
    import io
    import json
    from pathlib import Path
    from PIL import Image
    from lumen_core.svg_output import validate_static_svg
    from lumen_core.gif_editing import probe_gif, retime_gif, _parse
    root = Path(__file__).parent / "fixtures" / "image26-live"
    for item in json.loads((root / "manifest.json").read_text()):
        data = (root / item["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"]
        if item["file"].endswith(".svg"):
            assert list(validate_static_svg(data)) == item["dimensions"]
    original = (root / "gif-4frames-100ms.gif").read_bytes()
    retimed = retime_gif(original, 2)
    assert probe_gif(retimed)["durations_ms"] == [50] * 4
    allowed_offsets = {offset for frame in _parse(original).frames
                       for offset in (frame.delay_offset, frame.delay_offset + 1)}
    assert len(original) == len(retimed)
    assert all(i in allowed_offsets for i, (a, b) in enumerate(zip(original, retimed)) if a != b)
    with Image.open(io.BytesIO(original)) as before, Image.open(io.BytesIO(retimed)) as after:
        assert before.n_frames == after.n_frames == 4
        for i in range(4):
            before.seek(i)
            after.seek(i)
            assert before.convert("RGBA").tobytes() == after.convert("RGBA").tobytes()


def test_reported_author_top_level_reasoning_zero_is_not_dropped():
    import json
    from pathlib import Path
    payload = json.loads((Path(__file__).parent / "fixtures/image26-live/svg-usage-s03.json").read_text())
    assert public_svg_usage(payload["svg_usage"]) == payload["svg_usage"]
    assert public_svg_usage(payload["svg_usage"])["reasoning_tokens"] == 0
    assert payload["usage"]["total_tokens"] == 924
    assert payload["svg_usage"]["total_tokens"] == 4643
