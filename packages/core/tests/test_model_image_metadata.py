from __future__ import annotations

import io

from PIL import Image

from lumen_core.model_image_metadata import (
    build_model_image_metadata,
    model_image_filename,
    parse_model_image_filename,
    read_model_image_metadata,
    save_image_with_model_metadata,
)


def test_png_metadata_round_trips() -> None:
    payload = build_model_image_metadata(
        age_segment="adult",
        gender="female",
        appearance_direction="east_asian",
        style_tags=["清冷高级", "知性通勤"],
        source="model_library_generate",
        prompt_hint="studio reference",
    )
    buf = io.BytesIO()
    with Image.new("RGB", (2, 2), "white") as im:
        save_image_with_model_metadata(im, buf, fmt="PNG", metadata=payload)

    buf.seek(0)
    with Image.open(buf) as im:
        parsed = read_model_image_metadata(im)

    assert parsed is not None
    assert parsed.age_segment == "adult"
    assert parsed.gender == "female"
    assert parsed.appearance_direction == "east_asian"
    assert parsed.style_tags == ["清冷高级", "知性通勤"]
    assert parsed.source == "model_library_generate"


def test_jpeg_metadata_round_trips() -> None:
    payload = build_model_image_metadata(
        age_segment="adult",
        gender="male",
        appearance_direction="european",
        style_tags=["成熟稳重", "极简中性"],
        source="model_library_generate",
        prompt_hint="studio reference",
    )
    buf = io.BytesIO()
    with Image.new("RGB", (8, 8), "white") as im:
        save_image_with_model_metadata(
            im, buf, fmt="JPEG", metadata=payload, quality=90
        )

    buf.seek(0)
    with Image.open(buf) as im:
        parsed = read_model_image_metadata(im)

    assert parsed is not None
    assert parsed.age_segment == "adult"
    assert parsed.gender == "male"
    assert parsed.appearance_direction == "european"
    assert parsed.style_tags == ["成熟稳重", "极简中性"]
    assert parsed.source == "model_library_generate"


def test_model_image_filename_is_bounded_and_parseable() -> None:
    filename = model_image_filename(
        image_id="018f123456789abcdef",
        ext="png",
        age_segment="middle_aged",
        gender="male",
        appearance_direction="south_asian",
        style_tags=["成熟稳重", "复古文艺", "不会写入文件名"],
    )

    assert filename.endswith(".png")
    assert len(filename) <= 96
    parsed = parse_model_image_filename(filename)
    assert parsed is not None
    assert parsed.age_segment == "middle_aged"
    assert parsed.gender == "male"
    assert parsed.appearance_direction == "south_asian"
    assert parsed.style_tags == ["成熟稳重", "复古文艺"]
