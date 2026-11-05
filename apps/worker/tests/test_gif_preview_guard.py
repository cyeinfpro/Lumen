from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("frames", [17, 64])
@pytest.mark.asyncio
async def test_unconfirmed_gif_preview_frame_counts_reject_before_network(frames, monkeypatch):
    from app.upstream_parts import direct_requests

    def network_services(*args):
        pytest.fail("Unconfirmed GIF frame count must reject before network")

    monkeypatch.setattr(direct_requests, "_runtime_services", network_services)
    with pytest.raises(ValueError, match="between 2 and 16"):
        await direct_requests._direct_generate_image_once(
            SimpleNamespace(output_format="gif", size="512x512", frame_count=frames),
            base_url_override="https://test.invalid", api_key_override="test-unused",
        )
