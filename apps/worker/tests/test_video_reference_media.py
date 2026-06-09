from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.tasks import video_generation as video_generation_tasks
from app.tasks.video_generation import _reference_media_bytes, _try_provider_cancel
from app.video_upstream import (
    CancelResult,
    DashScopeHappyHorseAdapter,
    UnifiedVideoCreateAdapter,
    VideoReferenceMedia,
    VideoSubmitRequest,
    VideoUpstreamError,
    VolcanoSeedanceAdapter,
    VolcanoThirdPartySeedanceAdapter,
    _billable,
    _duration_usage_total_tokens,
    _usage_total_tokens,
    adapter_for_provider,
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
async def test_reference_media_bytes_preserves_image_url_snapshot_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = SimpleNamespace(
        action="reference",
        upstream_request={
            "reference_media": [
                {
                    "kind": "image",
                    "url": "https://lumen.example/api/images/reference/image-1/binary",
                    "upstream_reference_storage_key": "u/user-1/ref.jpg",
                    "upstream_reference_mime": "image/jpeg",
                }
            ]
        },
    )

    async def fake_get_bytes(key: str) -> bytes:
        assert key == "u/user-1/ref.jpg"
        return b"image"

    monkeypatch.setattr(video_generation_tasks.storage, "aget_bytes", fake_get_bytes)

    result = await _reference_media_bytes(generation)

    assert len(result) == 1
    assert result[0].kind == "image"
    assert result[0].url == "https://lumen.example/api/images/reference/image-1/binary"
    assert result[0].data == b"image"
    assert result[0].mime == "image/jpeg"


@pytest.mark.asyncio
async def test_reference_media_bytes_url_snapshot_survives_missing_variant_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = SimpleNamespace(
        action="reference",
        upstream_request={
            "reference_media": [
                {
                    "kind": "image",
                    "url": "https://lumen.example/api/images/reference/image-1/binary",
                    "upstream_reference_storage_key": "u/user-1/ref.jpg",
                    "upstream_reference_mime": "image/jpeg",
                }
            ]
        },
    )

    async def failing_get_bytes(key: str) -> bytes:
        raise FileNotFoundError(key)

    monkeypatch.setattr(
        video_generation_tasks.storage, "aget_bytes", failing_get_bytes
    )

    result = await _reference_media_bytes(generation)

    assert len(result) == 1
    assert result[0].kind == "image"
    assert result[0].url == "https://lumen.example/api/images/reference/image-1/binary"
    assert result[0].data is None
    assert result[0].mime == "image/jpeg"


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

    with pytest.raises(
        RuntimeError, match="reference video snapshot missing public URL"
    ):
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


class DashScopeCaptureClient(CaptureClient):
    def __init__(self, get_json: dict | None = None) -> None:
        super().__init__()
        self.get_json = get_json or {}

    async def post(self, path: str, *, json):
        self.path = path
        self.body = json
        return httpx.Response(200, json={"output": {"task_id": "hh-task-1"}})

    async def get(self, path: str):
        self.path = path
        return httpx.Response(200, json=self.get_json)


class ThirdPartyCaptureClient(CaptureClient):
    def __init__(
        self,
        *,
        post_json: dict | None = None,
        get_json: dict | None = None,
        delete_json: dict | None = None,
    ) -> None:
        super().__init__()
        self.post_json = post_json or {"task_id": "moyu-task-1"}
        self.get_json = get_json or {}
        self.delete_json = delete_json or {"code": "success"}
        self.params = None

    async def post(self, path: str, *, json):
        self.path = path
        self.body = json
        return httpx.Response(200, json=self.post_json)

    async def get(self, path: str, **kwargs):
        self.path = path
        self.params = kwargs.get("params")
        return httpx.Response(200, json=self.get_json)

    async def delete(self, path: str):
        self.path = path
        return httpx.Response(200, json=self.delete_json)


class SequentialThirdPartyCaptureClient(ThirdPartyCaptureClient):
    def __init__(self, responses: list[httpx.Response]) -> None:
        super().__init__()
        self.responses = responses
        self.requests: list[dict] = []

    async def post(self, path: str, *, json):
        self.path = path
        self.body = json
        self.requests.append({"path": path, "body": json})
        index = len(self.requests) - 1
        return self.responses[index]


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
            "url": ("https://lumen.example/api/videos/reference/video-1/binary?token=t")
        },
    }


@pytest.mark.asyncio
async def test_seedance_submit_forwards_asset_reference_url() -> None:
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

    await adapter.submit(
        VideoSubmitRequest(
            task_id="video-gen-1",
            user_id="user-1",
            action="reference",
            model="seedance-2.0",
            upstream_model="dreamina-seedance-2-0-260128",
            prompt="[Image 1]",
            duration_s=5,
            resolution="720p",
            aspect_ratio="adaptive",
            reference_media=[
                VideoReferenceMedia(
                    kind="image",
                    url="asset://asset-20260609161523-stlqd",
                )
            ],
        )
    )

    assert client.body["content"][1] == {
        "type": "image_url",
        "role": "reference_image",
        "image_url": {"url": "asset://asset-20260609161523-stlqd"},
    }


@pytest.mark.asyncio
async def test_volcano_third_party_submit_uses_moyu_video_generation_payload() -> None:
    provider = VideoProviderDefinition(
        name="moyu",
        kind="volcano_third_party",
        base_url="https://www.moyu.info",
        api_key="sk-test",
        models={"seedance-2.0-fast:reference": "doubao-seedance-2-0-fast-260128"},
    )
    adapter = VolcanoThirdPartySeedanceAdapter(provider)
    client = ThirdPartyCaptureClient()
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.submit(
        VideoSubmitRequest(
            task_id="video-gen-1",
            user_id="user-1",
            action="reference",
            model="seedance-2.0-fast",
            upstream_model="doubao-seedance-2-0-fast-260128",
            prompt="make it cinematic",
            duration_s=6,
            resolution="720p",
            aspect_ratio="16:9",
            generate_audio=True,
            reference_media=[
                VideoReferenceMedia(kind="image", data=b"image", mime="image/jpeg"),
            ],
        )
    )

    assert result.provider_task_id == "moyu-task-1"
    assert client.path == "v1/video/generations"
    assert client.body["model"] == "doubao-seedance-2-0-fast-260128"
    assert client.body["prompt"] == "make it cinematic"
    assert set(client.body) == {"model", "prompt", "metadata"}
    assert client.body["metadata"]["duration"] == 6
    assert client.body["metadata"]["resolution"] == "720p"
    assert client.body["metadata"]["ratio"] == "16:9"
    assert client.body["metadata"]["generate_audio"] is True
    assert client.body["metadata"]["content"][0] == {
        "type": "text",
        "text": "make it cinematic",
    }
    assert client.body["metadata"]["content"][1]["role"] == "reference_image"
    assert client.body["metadata"]["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )


@pytest.mark.asyncio
async def test_volcano_third_party_base_url_can_include_v1_path() -> None:
    provider = VideoProviderDefinition(
        name="moyu",
        kind="volcano_third_party",
        base_url="https://www.moyu.info/v1",
        api_key="sk-test",
        models={"seedance-2.0:t2v": "doubao-seedance-2-0-260128"},
    )
    adapter = VolcanoThirdPartySeedanceAdapter(provider)
    client = ThirdPartyCaptureClient()
    adapter._client = lambda: client  # type: ignore[method-assign]

    await adapter.submit(
        VideoSubmitRequest(
            task_id="video-gen-1",
            user_id="user-1",
            action="t2v",
            model="seedance-2.0",
            upstream_model="doubao-seedance-2-0-260128",
            prompt="a cat",
            duration_s=5,
            resolution="480p",
            aspect_ratio="adaptive",
        )
    )

    assert client.path == "video/generations"


@pytest.mark.asyncio
async def test_volcano_third_party_base_url_collapses_duplicate_v1_slashes() -> None:
    provider = VideoProviderDefinition(
        name="moyu",
        kind="volcano_third_party",
        base_url="https://www.moyu.info//v1",
        api_key="sk-test",
        models={"seedance-2.0:t2v": "doubao-seedance-2-0-260128"},
    )
    adapter = VolcanoThirdPartySeedanceAdapter(provider)

    assert adapter._path("video/generations") == "video/generations"
    async with adapter._client() as client:
        request = client.build_request("POST", adapter._path("video/generations"))
    assert str(request.url) == "https://www.moyu.info/v1/video/generations"


@pytest.mark.asyncio
async def test_volcano_third_party_poll_reads_moyu_result_shape() -> None:
    provider = VideoProviderDefinition(
        name="moyu",
        kind="volcano_third_party",
        base_url="https://www.moyu.info",
        api_key="sk-test",
        models={"seedance-2.0:t2v": "doubao-seedance-2-0-260128"},
    )
    adapter = VolcanoThirdPartySeedanceAdapter(provider)
    client = ThirdPartyCaptureClient(
        get_json={
            "code": "success",
            "data": {
                "task_id": "moyu-task-1",
                "status": "SUCCESS",
                "progress": "100%",
                "data": {
                    "content": {"video_url": "https://cdn.example/output.mp4"},
                    "usage": {"completion_tokens": 80770, "total_tokens": 90000},
                },
            },
        }
    )
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.poll("moyu-task-1")

    assert client.path == "v1/video/generations/moyu-task-1"
    assert result.status == "succeeded"
    assert result.progress == 100
    assert result.video_url == "https://cdn.example/output.mp4"
    assert result.usage_total_tokens == 80770
    assert result.upstream_billable is True


@pytest.mark.asyncio
async def test_volcano_third_party_poll_reads_live_moyu_wrapped_result_shape() -> None:
    provider = VideoProviderDefinition(
        name="moyu",
        kind="volcano_third_party",
        base_url="https://www.moyu.info",
        api_key="sk-test",
        models={"seedance-2.0-fast:reference": "doubao-seedance-2-0-fast-260128"},
    )
    adapter = VolcanoThirdPartySeedanceAdapter(provider)
    client = ThirdPartyCaptureClient(
        get_json={
            "code": "success",
            "data": {
                "action": "generate",
                "status": "SUCCESS",
                "task_id": "cgt-20260607183443-jmltj",
                "progress": "100%",
                "data": {
                    "code": "success",
                    "data": {
                        "id": "cgt-20260607183443-jmltj",
                        "status": "succeeded",
                        "content": {
                            "video_url": "https://cdn.example/live-output.mp4"
                        },
                        "usage": {
                            "completion_tokens": 130500,
                            "total_tokens": 130500,
                        },
                    },
                    "message": "",
                },
            },
            "message": "",
        }
    )
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.poll("cgt-20260607183443-jmltj")

    assert result.status == "succeeded"
    assert result.progress == 100
    assert result.video_url == "https://cdn.example/live-output.mp4"
    assert result.usage_total_tokens == 130500
    assert result.upstream_billable is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_url"),
    [
        (
            {"metadata": {"url": "https://cdn.example/metadata.mp4"}},
            "https://cdn.example/metadata.mp4",
        ),
        (
            {"data": {"result_url": "https://cdn.example/result.mp4"}},
            "https://cdn.example/result.mp4",
        ),
        (
            {"metadata": {"fetch_url": "/v1/videos/moyu-task-1/content"}},
            "https://www.moyu.info/v1/videos/moyu-task-1/content",
        ),
        (
            {"results": [{"url": "https://cdn.example/results.mp4"}]},
            "https://cdn.example/results.mp4",
        ),
    ],
)
async def test_volcano_third_party_poll_reads_common_result_url_shapes(
    payload: dict,
    expected_url: str,
) -> None:
    provider = VideoProviderDefinition(
        name="moyu",
        kind="volcano_third_party",
        base_url="https://www.moyu.info",
        api_key="sk-test",
        models={"seedance-2.0:t2v": "doubao-seedance-2-0-260128"},
    )
    adapter = VolcanoThirdPartySeedanceAdapter(provider)
    client = ThirdPartyCaptureClient(
        get_json={
            "code": "success",
            "data": {"task_id": "moyu-task-1", "status": "SUCCESS", **payload},
        }
    )
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.poll("moyu-task-1")

    assert result.status == "succeeded"
    assert result.video_url == expected_url


@pytest.mark.asyncio
async def test_volcano_third_party_poll_respects_explicit_billable_false() -> None:
    provider = VideoProviderDefinition(
        name="moyu",
        kind="volcano_third_party",
        base_url="https://www.moyu.info",
        api_key="sk-test",
        models={"seedance-2.0:t2v": "doubao-seedance-2-0-260128"},
    )
    adapter = VolcanoThirdPartySeedanceAdapter(provider)
    client = ThirdPartyCaptureClient(
        get_json={
            "code": "success",
            "data": {
                "task_id": "moyu-task-1",
                "status": "SUCCESS",
                "data": {
                    "content": {"video_url": "https://cdn.example/output.mp4"},
                    "usage": {"billable": False, "completion_tokens": 80770},
                },
            },
        }
    )
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.poll("moyu-task-1")

    assert result.status == "succeeded"
    assert result.upstream_billable is False


@pytest.mark.asyncio
async def test_unified_video_create_submit_uses_omni_flash_payload() -> None:
    provider = VideoProviderDefinition(
        name="google-omni-flash",
        kind="omni_flash",
        base_url="https://gateway.example.com",
        api_key="sk-test",
        models={"omni-flash:i2v": "gemini_omni_flash"},
    )
    adapter = UnifiedVideoCreateAdapter(provider)
    client = ThirdPartyCaptureClient(post_json={"data": {"task_id": "omni-task-1"}})
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.submit(
        VideoSubmitRequest(
            task_id="video-gen-1",
            user_id="user-1",
            action="i2v",
            model="omni-flash",
            upstream_model="gemini_omni_flash",
            prompt="make it cinematic",
            duration_s=6,
            resolution="720p",
            aspect_ratio="16:9",
            generate_audio=False,
            seed=123,
            input_image_url="https://lumen.example/ref.jpg",
        )
    )

    assert result.provider_task_id == "omni-task-1"
    assert client.path == "v1/video/create"
    assert client.body == {
        "model": "gemini_omni_flash",
        "prompt": "make it cinematic",
        "size": "720P",
        "aspect_ratio": "16:9",
        "duration": 6,
        "seed": 123,
        "generate_audio": False,
        "images": ["https://lumen.example/ref.jpg"],
    }


@pytest.mark.asyncio
async def test_unified_video_create_retries_invalid_url_with_data_urls() -> None:
    provider = VideoProviderDefinition(
        name="google-omni-flash",
        kind="omni_flash",
        base_url="https://gateway.example.com",
        api_key="sk-test",
        models={"omni-flash:reference": "gemini_omni_flash"},
    )
    adapter = UnifiedVideoCreateAdapter(provider)
    client = SequentialThirdPartyCaptureClient(
        [
            httpx.Response(400, json={"error": {"message": "Invalid URL"}}),
            httpx.Response(200, json={"data": {"task_id": "omni-task-1"}}),
        ]
    )
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.submit(
        VideoSubmitRequest(
            task_id="video-gen-1",
            user_id="user-1",
            action="reference",
            model="omni-flash",
            upstream_model="gemini_omni_flash",
            prompt="keep these references consistent",
            duration_s=6,
            resolution="720p",
            aspect_ratio="16:9",
            reference_media=[
                VideoReferenceMedia(
                    kind="image",
                    url="https://lumen.example/api/images/reference/image-1/binary",
                    data=b"image",
                    mime="image/jpeg",
                )
            ],
        )
    )

    assert result.provider_task_id == "omni-task-1"
    assert len(client.requests) == 2
    assert client.requests[0]["body"]["images"] == [
        "https://lumen.example/api/images/reference/image-1/binary"
    ]
    assert client.requests[1]["body"]["images"] == [
        "data:image/jpeg;base64,aW1hZ2U="
    ]


@pytest.mark.asyncio
async def test_unified_video_create_base_url_can_include_v1_path() -> None:
    provider = VideoProviderDefinition(
        name="google-omni-flash",
        kind="omni_flash",
        base_url="https://gateway.example.com/v1",
        api_key="sk-test",
        models={"omni-flash:t2v": "gemini_omni_flash"},
    )
    adapter = UnifiedVideoCreateAdapter(provider)
    client = ThirdPartyCaptureClient(post_json={"task_id": "omni-task-1"})
    adapter._client = lambda: client  # type: ignore[method-assign]

    await adapter.submit(
        VideoSubmitRequest(
            task_id="video-gen-1",
            user_id="user-1",
            action="t2v",
            model="omni-flash",
            upstream_model="gemini_omni_flash",
            prompt="a product shot",
            duration_s=5,
            resolution="480p",
            aspect_ratio="adaptive",
        )
    )

    assert client.path == "video/create"


@pytest.mark.asyncio
async def test_unified_video_create_poll_reads_query_result() -> None:
    provider = VideoProviderDefinition(
        name="google-omni-flash",
        kind="omni_flash",
        base_url="https://gateway.example.com",
        api_key="sk-test",
        models={"omni-flash:t2v": "gemini_omni_flash"},
    )
    adapter = UnifiedVideoCreateAdapter(provider)
    client = ThirdPartyCaptureClient(
        get_json={
            "data": {
                "status": "completed",
                "progress": 100,
                "video_urls": ["https://cdn.example/omni.mp4"],
                "usage": {"duration": 6},
            }
        }
    )
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.poll("omni-task-1")

    assert client.path == "v1/video/query"
    assert client.params == {"id": "omni-task-1"}
    assert result.status == "succeeded"
    assert result.progress == 100
    assert result.video_url == "https://cdn.example/omni.mp4"
    assert result.usage_total_tokens == 6_000_000
    assert result.upstream_billable is True


@pytest.mark.asyncio
async def test_volcano_third_party_poll_prefers_specific_failure_code() -> None:
    provider = VideoProviderDefinition(
        name="moyu",
        kind="volcano_third_party",
        base_url="https://www.moyu.info",
        api_key="sk-test",
        models={"seedance-2.0:t2v": "doubao-seedance-2-0-260128"},
    )
    adapter = VolcanoThirdPartySeedanceAdapter(provider)
    client = ThirdPartyCaptureClient(
        get_json={
            "code": "success",
            "data": {
                "task_id": "moyu-task-1",
                "status": "FAILURE",
                "fail_reason": "task failed",
                "data": {
                    "error": {
                        "code": "OutputVideoSensitiveContentDetected",
                        "message": "output video may contain sensitive information",
                    }
                },
            },
        }
    )
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.poll("moyu-task-1")

    assert result.status == "failed"
    assert result.failure_class == "content_policy"


@pytest.mark.asyncio
async def test_provider_cancel_retries_after_rejected_result() -> None:
    class Adapter:
        def __init__(self) -> None:
            self.calls = 0

        async def cancel(self, _provider_task_id: str) -> CancelResult:
            self.calls += 1
            return CancelResult(accepted=False, raw={"error": "not found"})

    adapter = Adapter()
    generation = SimpleNamespace(
        provider_task_id="moyu-task-1",
        diagnostics={},
    )

    await _try_provider_cancel(adapter, generation)
    await _try_provider_cancel(adapter, generation)

    assert adapter.calls == 2
    assert "cancel_sent_at" not in generation.diagnostics
    assert generation.diagnostics["cancel_rejected_at"]
    assert generation.diagnostics["cancel_result"] == {"error": "not found"}


@pytest.mark.asyncio
async def test_volcano_third_party_cancel_uses_moyu_cancel_path() -> None:
    provider = VideoProviderDefinition(
        name="moyu",
        kind="volcano_third_party",
        base_url="https://www.moyu.info",
        api_key="sk-test",
        models={"seedance-2.0:t2v": "doubao-seedance-2-0-260128"},
    )
    adapter = VolcanoThirdPartySeedanceAdapter(provider)
    client = ThirdPartyCaptureClient()
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.cancel("moyu-task-1")

    assert result is not None
    assert result.accepted is True
    assert client.path == "v1/videos/moyu-task-1"


def test_adapter_for_provider_selects_volcano_third_party_adapter() -> None:
    provider = VideoProviderDefinition(
        name="moyu",
        kind="volcano_third_party",
        base_url="https://www.moyu.info",
        api_key="sk-test",
        models={"seedance-2.0:t2v": "doubao-seedance-2-0-260128"},
    )

    assert isinstance(adapter_for_provider(provider), VolcanoThirdPartySeedanceAdapter)


def test_adapter_for_provider_selects_unified_video_create_adapter() -> None:
    provider = VideoProviderDefinition(
        name="google-omni-flash",
        kind="omni_flash",
        base_url="https://gateway.example.com",
        api_key="sk-test",
        models={"omni-flash:t2v": "gemini_omni_flash"},
    )

    assert isinstance(adapter_for_provider(provider), UnifiedVideoCreateAdapter)


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


def test_seedance_usage_reads_nested_task_response_usage() -> None:
    assert (
        _usage_total_tokens(
            {
                "data": {
                    "status": "succeeded",
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 108_044,
                        "total_tokens": 108_144,
                    },
                }
            }
        )
        == 108_044
    )


def test_seedance_usage_falls_back_to_nested_total_tokens() -> None:
    assert (
        _usage_total_tokens({"data": {"usage": {"total_tokens": 108_144}}}) == 108_144
    )


def test_seedance_billable_reads_nested_usage_flag() -> None:
    assert _billable({"data": {"usage": {"billable": False}}}) is False
    assert _billable({"result": {"billing": {"billable": "true"}}}) is True


@pytest.mark.asyncio
async def test_happyhorse_i2v_submit_uses_dashscope_media_payload() -> None:
    provider = VideoProviderDefinition(
        name="dashscope",
        kind="dashscope",
        base_url="https://dashscope-intl.aliyuncs.com",
        api_key="sk-test",
        models={"happyhorse-1.0:i2v": "happyhorse-1.0-i2v"},
    )
    adapter = DashScopeHappyHorseAdapter(provider)
    client = DashScopeCaptureClient()
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.submit(
        VideoSubmitRequest(
            task_id="video-gen-1",
            user_id="user-1",
            action="i2v",
            model="happyhorse-1.0",
            upstream_model="happyhorse-1.0-i2v",
            prompt="make it move",
            duration_s=3,
            resolution="720p",
            aspect_ratio="adaptive",
            input_image_url=(
                "https://lumen.example/api/images/reference/image-1/binary?token=t"
            ),
        )
    )

    assert result.provider_task_id == "hh-task-1"
    assert client.path == "/api/v1/services/aigc/video-generation/video-synthesis"
    assert client.body == {
        "model": "happyhorse-1.0-i2v",
        "input": {
            "prompt": "make it move",
            "media": [
                {
                    "type": "first_frame",
                    "url": (
                        "https://lumen.example/api/images/reference/image-1/binary?token=t"
                    ),
                }
            ],
        },
        "parameters": {
            "duration": 3,
            "resolution": "720P",
            "watermark": False,
        },
    }


@pytest.mark.asyncio
async def test_happyhorse_reference_submit_uses_reference_image_media_payload() -> None:
    provider = VideoProviderDefinition(
        name="dashscope",
        kind="dashscope",
        base_url="https://dashscope-intl.aliyuncs.com",
        api_key="sk-test",
        models={"happyhorse-1.0:reference": "happyhorse-1.0-r2v"},
    )
    adapter = DashScopeHappyHorseAdapter(provider)
    client = DashScopeCaptureClient()
    adapter._client = lambda: client  # type: ignore[method-assign]

    await adapter.submit(
        VideoSubmitRequest(
            task_id="video-gen-1",
            user_id="user-1",
            action="reference",
            model="happyhorse-1.0",
            upstream_model="happyhorse-1.0-r2v",
            prompt="match this product",
            duration_s=-1,
            resolution="1080p",
            aspect_ratio="9:16",
            seed=123,
            reference_media=[
                VideoReferenceMedia(
                    kind="image",
                    url=(
                        "https://lumen.example/api/images/reference/image-1/binary?token=t"
                    ),
                )
            ],
        )
    )

    assert client.body["input"]["media"] == [
        {
            "type": "reference_image",
            "url": (
                "https://lumen.example/api/images/reference/image-1/binary?token=t"
            ),
        }
    ]
    assert client.body["parameters"] == {
        "resolution": "1080P",
        "watermark": False,
        "ratio": "9:16",
        "seed": 123,
    }


@pytest.mark.asyncio
async def test_happyhorse_reference_rejects_reference_video() -> None:
    provider = VideoProviderDefinition(
        name="dashscope",
        kind="dashscope",
        base_url="https://dashscope-intl.aliyuncs.com",
        api_key="sk-test",
        models={"happyhorse-1.0:reference": "happyhorse-1.0-r2v"},
    )
    adapter = DashScopeHappyHorseAdapter(provider)

    with pytest.raises(VideoUpstreamError, match="does not support reference videos"):
        await adapter.submit(
            VideoSubmitRequest(
                task_id="video-gen-1",
                user_id="user-1",
                action="reference",
                model="happyhorse-1.0",
                upstream_model="happyhorse-1.0-r2v",
                prompt="match this product",
                duration_s=5,
                resolution="720p",
                aspect_ratio="adaptive",
                reference_media=[
                    VideoReferenceMedia(
                        kind="video",
                        url="https://lumen.example/reference.mp4",
                    )
                ],
            )
        )


def test_happyhorse_usage_duration_maps_seconds_to_internal_tokens() -> None:
    assert (
        _duration_usage_total_tokens({"output": {"usage": {"duration": 3}}})
        == 3_000_000
    )
    assert (
        _duration_usage_total_tokens({"usage": {"output_video_duration": "5.5"}})
        == 5_500_000
    )


@pytest.mark.asyncio
async def test_happyhorse_poll_reads_result_url_and_billable_duration() -> None:
    provider = VideoProviderDefinition(
        name="dashscope",
        kind="dashscope",
        base_url="https://dashscope-intl.aliyuncs.com",
        api_key="sk-test",
        models={"happyhorse-1.0:t2v": "happyhorse-1.0-t2v"},
    )
    adapter = DashScopeHappyHorseAdapter(provider)
    client = DashScopeCaptureClient(
        {
            "output": {
                "task_status": "SUCCEEDED",
                "results": [{"url": "https://cdn.example/output.mp4"}],
                "usage": {"duration": "3.5"},
            }
        }
    )
    adapter._client = lambda: client  # type: ignore[method-assign]

    result = await adapter.poll("hh-task-1")

    assert client.path == "/api/v1/tasks/hh-task-1"
    assert result.status == "succeeded"
    assert result.video_url == "https://cdn.example/output.mp4"
    assert result.usage_total_tokens == 3_500_000
    assert result.upstream_billable is True


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
