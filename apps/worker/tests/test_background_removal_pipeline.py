from __future__ import annotations

import pytest
from PIL import Image as PILImage

from app.background_removal import pipeline
from app.background_removal.types import BackgroundRemovalResult, TransparentQcReport


@pytest.mark.asyncio
async def test_provider_exception_falls_through_to_next_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenProvider:
        name = "broken"

        async def remove_background(self, *_args, **_kwargs):
            raise RuntimeError("provider unavailable")

    class WorkingProvider:
        name = "working"

        async def remove_background(self, *_args, **_kwargs):
            rgba = PILImage.new("RGBA", (4, 4), (10, 20, 30, 255))
            alpha = PILImage.new("L", (4, 4), 255)
            return BackgroundRemovalResult(
                rgba=rgba,
                alpha_mask=alpha,
                provider=self.name,
            )

    monkeypatch.setattr(
        pipeline.alpha_refine,
        "refine",
        lambda image: image.copy(),
    )
    monkeypatch.setattr(
        pipeline.qc,
        "evaluate",
        lambda _image: TransparentQcReport(
            passed=True,
            score=1.0,
            failure_reasons=[],
            warnings=[],
            foreground_bbox=(0, 0, 4, 4),
            alpha_coverage=1.0,
            border_alpha_max=255,
            largest_component_ratio=1.0,
        ),
    )

    source = PILImage.new("RGB", (4, 4), (10, 20, 30))
    try:
        result = await pipeline.process_transparent_request(
            source,
            providers=(BrokenProvider(), WorkingProvider()),
        )
    finally:
        source.close()

    assert result.provider == "working"
    assert result.width == 4
    assert result.height == 4
