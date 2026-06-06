from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.tasks.video_generation import _reference_media_bytes
from app.video_upstream import (
    VideoReferenceMedia,
    VideoSubmitRequest,
    VideoUpstreamError,
    VolcanoSeedanceAdapter,
    _usage_total_tokens,
)
from lumen_core.video_providers import VideoProviderDefinition


@pytest.mark.asyncio
async def test_reference_media_bytes_requires_snapshot() -> None:
    generation = SimpleNamespace(action="reference", upstream_request={})

    with pytest.raises(RuntimeError, match="reference media snapshot missing"):
        await _reference_media_bytes(generation)


@pytest.mark.asyncio
async def test_reference_media_bytes_accepts_url_snapshots() -> None:
    generation = SimpleNamespace(
        action="reference",
        upstream_request={
            "reference_media": [
                {
                    "kind": "video",
                    "url": "https://example.com/reference.mp4",
                }
            ]
        },
    )

    result = await _reference_media_bytes(generation)

    assert len(result) == 1
    assert result[0].kind == "video"
    assert result[0].url == "https://example.com/reference.mp4"


@pytest.mark.asyncio
async def test_reference_media_bytes_rejects_local_video_snapshots() -> None:
    generation = SimpleNamespace(
        action="reference",
        upstream_request={
            "reference_media": [
                {
                    "kind": "video",
                    "storage_key": "u/user-1/vref/video-1/original.mp4",
                    "mime": "video/mp4",
                }
            ]
        },
    )

    with pytest.raises(RuntimeError, match="reference video snapshot missing public URL"):
        await _reference_media_bytes(generation)


class CaptureClient:
    def __init__(self) -> None:
        self.body = None
        self.path = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def post(self, path: str, *, json):
        self.path = path
        self.body = json
        return httpx.Response(200, json={"id": "seedance-task-1"})


@pytest.mark.asyncio
async def test_seedance_submit_uses_official_reference_payload_without_fps() -> None:
    provider = VideoProviderDefinition(
        name="volcano",
        kind="volcano",
        base_url="https://ark.example/api/v3",
        api_key="sk-test",
        models={"seedance-2.0:reference": "dreamina-seedance-2-0-260128"},
    )
    adapter = VolcanoSeedanceAdapter(provider)
    client = CaptureClient()
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.submit(
        VideoSubmitRequest(
            task_id="video-gen-1",
            user_id="user-1",
            action="reference",
            model="seedance-2.0",
            upstream_model="dreamina-seedance-2-0-260128",
            prompt="[Image 1] and [Video 1]",
            duration_s=5,
            resolution="720p",
            aspect_ratio="adaptive",
            generate_audio=True,
            watermark=False,
            reference_media=[
                VideoReferenceMedia(
                    kind="image",
                    data=b"image",
                    mime="image/png",
                ),
                VideoReferenceMedia(
                    kind="video",
                    url=(
                        "https://lumen.example/api/videos/reference/video-1/binary"
                        "?token=t"
                    ),
                ),
            ],
        )
    )

    assert result.provider_task_id == "seedance-task-1"
    assert client.path == "/contents/generations/tasks"
    assert client.body["model"] == "dreamina-seedance-2-0-260128"
    assert client.body["ratio"] == "adaptive"
    assert client.body["watermark"] is False
    assert len(client.body["safety_identifier"]) == 64
    assert "fps" not in client.body
    assert client.body["content"][1]["role"] == "reference_image"
    assert client.body["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert client.body["content"][2] == {
        "type": "video_url",
        "role": "reference_video",
        "video_url": {
            "url": (
                "https://lumen.example/api/videos/reference/video-1/binary?token=t"
            )
        },
    }


@pytest.mark.asyncio
async def test_seedance_submit_rejects_reference_video_without_url() -> None:
    provider = VideoProviderDefinition(
        name="volcano",
        kind="volcano",
        base_url="https://ark.example/api/v3",
        api_key="sk-test",
        models={"seedance-2.0:reference": "dreamina-seedance-2-0-260128"},
    )
    adapter = VolcanoSeedanceAdapter(provider)

    with pytest.raises(VideoUpstreamError, match="public URL or asset ID"):
        await adapter.submit(
            VideoSubmitRequest(
                task_id="video-gen-1",
                user_id="user-1",
                action="reference",
                model="seedance-2.0",
                upstream_model="dreamina-seedance-2-0-260128",
                prompt="[Video 1]",
                duration_s=5,
                resolution="720p",
                aspect_ratio="adaptive",
                reference_media=[
                    VideoReferenceMedia(
                        kind="video",
                        data=b"video",
                        mime="video/mp4",
                    )
                ],
            )
        )


def test_seedance_usage_prefers_official_completion_tokens() -> None:
    assert (
        _usage_total_tokens(
            {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 42_000,
                    "total_tokens": 42_100,
                }
            }
        )
        == 42_000
    )


@pytest.mark.asyncio
async def test_seedance_submit_forwards_smart_duration() -> None:
    provider = VideoProviderDefinition(
        name="volcano",
        kind="volcano",
        base_url="https://ark.example/api/v3",
        api_key="sk-test",
        models={"seedance-2.0:t2v": "dreamina-seedance-2-0-260128"},
    )
    adapter = VolcanoSeedanceAdapter(provider)
    client = CaptureClient()
    adapter._client = lambda: client  # type: ignore[method-assign]

    await adapter.submit(
        VideoSubmitRequest(
            task_id="video-gen-1",
            user_id="user-1",
            action="t2v",
            model="seedance-2.0",
            upstream_model="dreamina-seedance-2-0-260128",
            prompt="a cat",
            duration_s=-1,
            resolution="720p",
            aspect_ratio="adaptive",
            generate_audio=True,
            watermark=False,
        )
    )

    assert client.body["duration"] == -1
