"""Inpaint mask tests covering upstream multipart, prompt wrap, provider ordering,
and generation.py mask resize.

Layered:
- ``_wrap_inpaint_prompt`` 模板包装（OpenAI 推荐 invariant 写法，spike 已验证）。
- ``_direct_edit_image_once`` multipart 字段名 ``mask``（不是 ``mask[]``）。
- ``ProviderPool.select(requires_mask=True)`` 优先 file transport，
  file 候选耗尽时回退 url transport。
- ``_resize_mask_to_reference`` LANCZOS 缩放对齐。
- ``edit_image()`` 顶层透传 mask + prompt wrap。
"""

from __future__ import annotations

import io as _io
import time
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as _PILImage

from app import upstream
from app.provider_pool import ProviderConfig, ProviderHealth, ProviderPool
from app.tasks import generation
from app.tasks.generation_parts import references
from lumen_core.constants import GenerationErrorCode as EC


def _make_png(size: tuple[int, int] = (8, 8), color: tuple[int, int, int] = (200, 200, 200)) -> bytes:
    buf = _io.BytesIO()
    _PILImage.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def _make_mask_png(size: tuple[int, int] = (8, 8)) -> bytes:
    """RGBA mask；OpenAI 约定 alpha=0 是要重画的区域，alpha=255 是保留区域。

    用 alpha=0 全图是测试 fast path（同尺寸 + 二值 alpha → 直返字节）的最小输入。
    """
    buf = _io.BytesIO()
    _PILImage.new("RGBA", size, color=(0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _make_partial_alpha_mask_png(size: tuple[int, int] = (8, 8), alpha: int = 128) -> bytes:
    """RGBA mask 但 alpha 为 partial（1-254 之间）；用于测试 worker 兜底二值化。"""
    buf = _io.BytesIO()
    _PILImage.new("RGBA", size, color=(255, 255, 255, alpha)).save(buf, format="PNG")
    return buf.getvalue()


# --- _wrap_inpaint_prompt --------------------------------------------------


def test_wrap_inpaint_prompt_keeps_user_intent_inside() -> None:
    wrapped = upstream._wrap_inpaint_prompt("add a red hat")
    # 关键不变量：前缀 / 后缀稳定，user intent 夹在中间。
    assert wrapped.startswith("Inside the masked region, add a red hat")
    assert "Preserve everything outside the mask exactly" in wrapped
    assert "Do not add anything outside the masked area." in wrapped


def test_wrap_inpaint_prompt_includes_blend_directive() -> None:
    """第四行 fill-context 让 remove/replace 类指令下模型用周围像素自然过渡填充，
    避免"Inside the masked region, remove the apple." 类 prompt 模型填黑/灰。
    """
    wrapped = upstream._wrap_inpaint_prompt("remove the apple")
    assert "Blend the result seamlessly with the surrounding unchanged area." in wrapped


def test_wrap_inpaint_prompt_strips_user_intent() -> None:
    # 用户输入两端空白不应改变模板形态（cache prefix 稳定的前提）。
    wrapped = upstream._wrap_inpaint_prompt("   add a hat   ")
    assert wrapped.startswith("Inside the masked region, add a hat.")


def test_wrap_inpaint_prompt_prefix_is_stable_for_cache() -> None:
    # 不同 user_intent → 前缀（"Inside the masked region, "）字面完全一致。
    a = upstream._wrap_inpaint_prompt("foo")
    b = upstream._wrap_inpaint_prompt("bar baz")
    assert a.split(",", 1)[0] == b.split(",", 1)[0] == "Inside the masked region"


# --- _direct_edit_image_once multipart includes mask ----------------------


@pytest.mark.asyncio
async def test_direct_edit_image_once_includes_mask_field(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_curl_post_multipart(*, url: str, data: dict[str, str],
                                       files: list[Any], headers: dict[str, str],
                                       timeout_s: float, proxy_url: str | None = None) -> tuple[int, dict[str, Any]]:
        captured["files"] = files
        captured["data"] = data
        # Return a minimal valid OpenAI image response payload
        return 200, {
            "data": [
                {
                    "b64_json": "aGVsbG8=",  # noqa: S105 - test fixture
                }
            ],
        }

    async def fake_resolve_runtime(*_a: Any, **_k: Any) -> tuple[str, str]:
        return "https://upstream.example", "sk-test"

    async def fake_resolve_proxy(*_a: Any, **_k: Any) -> str | None:
        return None

    monkeypatch.setattr(upstream, "_curl_post_multipart", fake_curl_post_multipart)
    monkeypatch.setattr(upstream, "_resolve_runtime", fake_resolve_runtime)
    monkeypatch.setattr(upstream, "resolve_provider_proxy_url", fake_resolve_proxy)

    img = _make_png()
    mask = _make_mask_png()

    await upstream._direct_edit_image_once(
        prompt="hi",
        size="1024x1024",
        images=[img],
        mask=mask,
        n=1,
        quality="high",
        output_format=None,
        output_compression=None,
        background=None,
        moderation=None,
        base_url_override="https://upstream.example",
        api_key_override="sk-test",
    )

    files = captured["files"]
    field_names = [name for name, _ in files]
    assert field_names == ["image[]", "mask"], (
        f"expected single 'mask' field after image[] entries, got {field_names}"
    )
    mask_entry = next(entry for name, entry in files if name == "mask")
    filename, raw, mime = mask_entry
    assert filename == "mask.png"
    assert raw == mask
    assert mime == "image/png"


@pytest.mark.asyncio
async def test_direct_edit_image_once_omits_mask_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """mask=None 时不带这个字段，保持现有 i2i 行为。"""
    captured: dict[str, Any] = {}

    async def fake_curl_post_multipart(*, url: str, data: dict[str, str],
                                       files: list[Any], headers: dict[str, str],
                                       timeout_s: float, proxy_url: str | None = None) -> tuple[int, dict[str, Any]]:
        captured["files"] = files
        return 200, {"data": [{"b64_json": "aGVsbG8="}]}  # noqa: S105

    async def fake_resolve_proxy(*_a: Any, **_k: Any) -> str | None:
        return None

    monkeypatch.setattr(upstream, "_curl_post_multipart", fake_curl_post_multipart)
    monkeypatch.setattr(upstream, "resolve_provider_proxy_url", fake_resolve_proxy)

    await upstream._direct_edit_image_once(
        prompt="hi",
        size="1024x1024",
        images=[_make_png()],
        mask=None,
        n=1,
        quality="high",
        output_format=None,
        output_compression=None,
        background=None,
        moderation=None,
        base_url_override="https://upstream.example",
        api_key_override="sk-test",
    )

    field_names = [name for name, _ in captured["files"]]
    assert field_names == ["image[]"], (
        f"mask=None should not add mask field, got {field_names}"
    )


# --- ProviderPool.select(requires_mask=True) ------------------------------


def _make_pool(*configs: ProviderConfig) -> ProviderPool:
    pool = ProviderPool()
    pool._providers = list(configs)
    pool._health = {p.name: ProviderHealth() for p in configs}
    pool._config_loaded_at = time.monotonic() + 60.0
    return pool


def _cfg(name: str, *, transport: str = "url") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        base_url=f"https://{name}.example",
        api_key=f"sk-{name}",
        priority=0,
        weight=1,
        enabled=True,
        image_edit_input_transport=transport,
    )


@pytest.mark.asyncio
async def test_select_requires_mask_prefers_file_transport_providers() -> None:
    pool = _make_pool(
        _cfg("url-only", transport="url"),
        _cfg("file-capable", transport="file"),
    )
    providers = await pool.select(route="image", requires_mask=True)
    names = {p.name for p in providers}
    assert names == {"file-capable"}, (
        f"requires_mask=True should prefer file-transport providers, got {names}"
    )


@pytest.mark.asyncio
async def test_select_requires_mask_falls_back_to_url_transport() -> None:
    """全部候选 transport=url → 返回 url 候选，避免 file 池耗尽时直接终态。"""
    pool = _make_pool(
        _cfg("url-a", transport="url"),
        _cfg("url-b", transport="url"),
    )
    providers = await pool.select(route="image", requires_mask=True)
    assert {p.name for p in providers} == {"url-a", "url-b"}


@pytest.mark.asyncio
async def test_select_without_requires_mask_keeps_all_transports() -> None:
    """非 inpaint 任务（mask=None → requires_mask=False）不被新过滤影响。"""
    pool = _make_pool(
        _cfg("url-a", transport="url"),
        _cfg("file-a", transport="file"),
    )
    providers = await pool.select(route="image")
    assert {p.name for p in providers} == {"url-a", "file-a"}


# --- Bug 2: mask_transport_required=False 不按 transport 过滤（direct 路径） ---


@pytest.mark.asyncio
async def test_select_mask_transport_not_required_keeps_url_providers() -> None:
    """direct edits 路径调 select(requires_mask=True, mask_transport_required=False)
    时不应过滤 transport=url 的号——direct multipart 自己处理 mask binary。"""
    pool = _make_pool(
        _cfg("url-a", transport="url"),
        _cfg("file-a", transport="file"),
    )
    providers = await pool.select(
        route="image",
        requires_mask=True,
        mask_transport_required=False,
    )
    assert {p.name for p in providers} == {"url-a", "file-a"}


@pytest.mark.asyncio
async def test_select_mask_transport_required_default_filters() -> None:
    """sidecar 路径默认 mask_transport_required=True：file-mode 优先。"""
    pool = _make_pool(
        _cfg("url-a", transport="url"),
        _cfg("file-a", transport="file"),
    )
    providers = await pool.select(route="image", requires_mask=True)
    assert {p.name for p in providers} == {"file-a"}


@pytest.mark.asyncio
async def test_pool_select_compat_mask_transport_false_keeps_url_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upstream._pool_select_compat 兜底过滤层也应该在 mask_transport_required=False
    时跳过 transport 过滤——避免 direct 路径被 url 模式 provider 错杀。"""
    pool = _make_pool(
        _cfg("url-a", transport="url"),
        _cfg("file-a", transport="file"),
    )
    providers = await upstream._pool_select_compat(
        pool,
        route="image",
        requires_mask=True,
        mask_transport_required=False,
    )
    assert {p.name for p in providers} == {"url-a", "file-a"}


@pytest.mark.asyncio
async def test_pool_select_compat_legacy_mock_mask_transport_false_no_filter() -> None:
    """老 mock 不识别 mask_transport_required / requires_mask 时，本地 fallback
    过滤要尊重 mask_transport_required=False（不过滤 transport）。"""

    class LegacyPool:
        async def select(self, **_kwargs: Any) -> list[Any]:
            # Mock 故意只接受最少 kwargs，保留对应过滤逻辑给上层兜底。
            return [
                _cfg("url-a", transport="url"),
                _cfg("file-a", transport="file"),
            ]

    providers = await upstream._pool_select_compat(
        LegacyPool(),
        route="image",
        requires_mask=True,
        mask_transport_required=False,
    )
    assert {p.name for p in providers} == {"url-a", "file-a"}


# --- _resize_mask_to_reference ---------------------------------------------


def test_resize_mask_keeps_bytes_when_already_aligned() -> None:
    """同尺寸 + 合法 mask 形态 + alpha 已二值 → 字节直返（避免无谓重编码）。"""
    ref = _make_png(size=(64, 64))
    mask = _make_mask_png(size=(64, 64))  # alpha=0 全图，已二值
    out = generation._resize_mask_to_reference(mask, ref)
    assert out is mask or out == mask


def test_reference_facade_keeps_pure_helper_aliases() -> None:
    assert generation._mask_alpha_is_binary is references.mask_alpha_is_binary
    assert generation._reference_pixel_size is references.reference_pixel_size


def test_resize_mask_facade_injects_current_alpha_checker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = _make_png(size=(32, 32))
    mask = _make_partial_alpha_mask_png(size=(32, 32), alpha=128)
    monkeypatch.setattr(generation, "_mask_alpha_is_binary", lambda _image: True)

    assert generation._resize_mask_to_reference(mask, ref) == mask


def test_resize_mask_resamples_when_size_mismatch() -> None:
    ref = _make_png(size=(128, 96))
    mask = _make_mask_png(size=(64, 64))
    out = generation._resize_mask_to_reference(mask, ref)
    # 输出必须能被 PIL 解开且尺寸 = 参考图。
    with _PILImage.open(_io.BytesIO(out)) as resized:
        assert resized.size == (128, 96)
        # mask 输出统一 RGBA（保 alpha 通道）。
        assert resized.mode == "RGBA"


def test_resize_mask_rejects_invalid_bytes() -> None:
    ref = _make_png(size=(32, 32))
    with pytest.raises(upstream.UpstreamError) as ei:
        generation._resize_mask_to_reference(b"not-a-png", ref)
    assert ei.value.error_code == EC.BAD_REFERENCE_IMAGE.value


def test_resize_mask_rejects_invalid_reference_bytes_without_mask_false_positive() -> None:
    """A bad reference must not be misreported as a mask resize or retriable error."""
    mask = _make_mask_png(size=(32, 32))
    with pytest.raises(upstream.UpstreamError) as ei:
        generation._resize_mask_to_reference(mask, b"not-a-png")

    assert ei.value.error_code == EC.BAD_REFERENCE_IMAGE.value
    assert "reference image not decodable" in str(ei.value)
    assert "mask image not decodable" not in str(ei.value)


def test_resize_mask_binarizes_partial_alpha_same_size() -> None:
    """同尺寸但 alpha=128 (partial) → fast path miss，要二值化兜底。

    OpenAI /v1/images/edits 只在 alpha=0/255 时定义；前端 destination-out 描线
    1-px 抗锯齿会留 partial alpha，worker 必须压回二值。
    """
    ref = _make_png(size=(32, 32))
    mask = _make_partial_alpha_mask_png(size=(32, 32), alpha=128)

    out = generation._resize_mask_to_reference(mask, ref)
    # 不能直返原字节（alpha 不二值，必须重编码）
    assert out != mask

    with _PILImage.open(_io.BytesIO(out)) as resized:
        alpha = resized.getchannel("A")
        lo, hi = alpha.getextrema()
        # alpha=128 经阈值化（>= 128 → 255），全图 alpha=255
        assert (lo, hi) == (255, 255), (
            f"alpha must be binarized (128 → 255 since >= threshold), got {(lo, hi)}"
        )


# --- normalized_ref loading -----------------------------------------------


@pytest.mark.asyncio
async def test_load_reference_images_prefers_normalized_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Rows:
        def all(self) -> list[Any]:
            return [
                SimpleNamespace(
                    id="img-1",
                    storage_key="u/user/uploads/img-1.png",
                    sha256="orig-sha",
                    metadata_jsonb={
                        "normalized_ref": {
                            "storage_key": "u/user/uploads/img-1.ref.webp",
                            "sha256": "ref-sha",
                        }
                    },
                )
            ]

    class _Session:
        async def execute(self, _stmt: Any) -> _Rows:
            return _Rows()

    calls: list[str] = []

    async def fake_aget_bytes(key: str) -> bytes:
        calls.append(key)
        return b"normalized-reference"

    monkeypatch.setattr(generation.storage, "aget_bytes", fake_aget_bytes)

    refs = await generation._load_reference_images(_Session(), ["img-1"])

    assert refs == [("ref-sha", b"normalized-reference")]
    assert calls == ["u/user/uploads/img-1.ref.webp"]


@pytest.mark.asyncio
async def test_load_reference_images_falls_back_when_normalized_ref_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Rows:
        def all(self) -> list[Any]:
            return [
                SimpleNamespace(
                    id="img-1",
                    storage_key="u/user/uploads/img-1.png",
                    sha256="orig-sha",
                    metadata_jsonb={
                        "normalized_ref": {
                            "storage_key": "u/user/uploads/img-1.ref.webp",
                            "sha256": "ref-sha",
                        }
                    },
                )
            ]

    class _Session:
        async def execute(self, _stmt: Any) -> _Rows:
            return _Rows()

    calls: list[str] = []
    warnings: list[tuple[object, ...]] = []

    async def fake_aget_bytes(key: str) -> bytes:
        calls.append(key)
        if key.endswith(".ref.webp"):
            raise FileNotFoundError(key)
        return b"original-reference"

    class _Logger:
        def warning(self, *args: object) -> None:
            warnings.append(args)

    monkeypatch.setattr(generation.storage, "aget_bytes", fake_aget_bytes)
    monkeypatch.setattr(generation, "logger", _Logger())

    refs = await generation._load_reference_images(_Session(), ["img-1"])

    assert refs == [("orig-sha", b"original-reference")]
    assert calls == ["u/user/uploads/img-1.ref.webp", "u/user/uploads/img-1.png"]
    assert warnings and warnings[0][1:] == (
        "img-1",
        "u/user/uploads/img-1.ref.webp",
        "u/user/uploads/img-1.png",
    )


@pytest.mark.asyncio
async def test_load_mask_image_uses_current_storage_and_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Rows:
        def first(self) -> Any:
            return SimpleNamespace(storage_key="u/user/mask.png")

    class _Session:
        async def execute(self, _stmt: Any) -> _Rows:
            return _Rows()

    class _Storage:
        async def aget_bytes(self, key: str) -> bytes:
            assert key == "u/user/mask.png"
            return b"four"

    monkeypatch.setattr(generation, "storage", _Storage())
    monkeypatch.setattr(generation, "_MASK_MAX_BYTES", 3)

    with pytest.raises(upstream.UpstreamError) as exc_info:
        await generation._load_mask_image(_Session(), "mask-1")

    assert exc_info.value.error_code == EC.REFERENCE_IMAGE_TOO_LARGE.value
    assert exc_info.value.payload == {"max_bytes": 3, "actual_bytes": 4}


def test_resize_mask_binarizes_partial_alpha_below_threshold() -> None:
    """alpha=64 < 128 阈值 → 全部归 0（"重画整张"——是 mask 全涂的合法语义）。"""
    ref = _make_png(size=(32, 32))
    mask = _make_partial_alpha_mask_png(size=(32, 32), alpha=64)

    out = generation._resize_mask_to_reference(mask, ref)

    with _PILImage.open(_io.BytesIO(out)) as resized:
        alpha = resized.getchannel("A")
        lo, hi = alpha.getextrema()
        assert (lo, hi) == (0, 0), (
            f"alpha=64 (< 128) must binarize to 0 everywhere, got {(lo, hi)}"
        )


def test_resize_mask_uses_nearest_keeps_binary_after_upsample() -> None:
    """4x4 半涂 mask（左 alpha=0，右 alpha=255）放大到 8x8 后边界仍是 {0, 255}。

    LANCZOS 旧实现会在边界引入 alpha=128 灰带；改 NEAREST + 二值化后保证不会。
    """
    halved = _PILImage.new("RGBA", (4, 4))
    for y in range(4):
        for x in range(4):
            halved.putpixel((x, y), (255, 255, 255, 0 if x < 2 else 255))
    buf = _io.BytesIO()
    halved.save(buf, format="PNG")
    mask = buf.getvalue()

    ref = _make_png(size=(8, 8))
    out = generation._resize_mask_to_reference(mask, ref)

    with _PILImage.open(_io.BytesIO(out)) as resized:
        alpha = resized.getchannel("A")
        for y in range(resized.height):
            for x in range(resized.width):
                a = alpha.getpixel((x, y))
                assert a in (0, 255), (
                    f"non-binary alpha at ({x},{y}): {a} — NEAREST + binarize broken"
                )


# --- _inpaint_size_from_reference -----------------------------------------


def test_inpaint_size_from_reference_uses_ref_dims_when_valid() -> None:
    """1024x768 已经 16-aligned + 像素 / 长宽比 / 长边都合法 → 直接拿 ref 尺寸用。"""
    assert generation._inpaint_size_from_reference(1024, 768) == "1024x768"


def test_inpaint_size_from_reference_aligns_to_16() -> None:
    """1023x767 → 最近 16 倍数 1024x768。"""
    assert generation._inpaint_size_from_reference(1023, 767) == "1024x768"


def test_inpaint_size_from_reference_clamps_long_side_and_pixel_budget() -> None:
    """长边 > 3840 OR 总像素 > 8.29M → 双重缩到合法区间；4096x3072 (4:3) 受像素
    预算限制（3840x2880 = 11M 超 budget）→ 进一步缩到 3312x2480 ≈ 8.2M。"""
    out = generation._inpaint_size_from_reference(4096, 3072)
    assert out is not None
    w, h = map(int, out.split("x"))
    assert max(w, h) <= 3840
    assert w * h <= 8_294_400
    assert w % 16 == 0 and h % 16 == 0
    # 比例和原图相近（≤ 1%）
    assert abs((w / h) - (4096 / 3072)) / (4096 / 3072) < 0.01


def test_inpaint_size_from_reference_scales_up_below_min_pixels() -> None:
    """像素 < 655360 → 等比放大到刚好 ≥ 阈值；100x100 → 832x832（16-aligned）。"""
    out = generation._inpaint_size_from_reference(100, 100)
    assert out is not None
    w, h = map(int, out.split("x"))
    assert w * h >= 655_360
    assert w == h  # 1:1 比例保持
    assert w % 16 == 0 and h % 16 == 0


def test_inpaint_size_from_reference_returns_none_for_extreme_aspect() -> None:
    """长宽比 > 21:9 (~2.33) → 返回 None 让 caller 回退到 resolved.size。"""
    assert generation._inpaint_size_from_reference(3000, 100) is None


def test_inpaint_size_from_reference_returns_none_for_zero_dims() -> None:
    """非法 0/负数 dim → None。"""
    assert generation._inpaint_size_from_reference(0, 100) is None
    assert generation._inpaint_size_from_reference(100, 0) is None
    assert generation._inpaint_size_from_reference(-1, 100) is None


def test_inpaint_size_facade_uses_current_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation, "MAX_EXPLICIT_ASPECT", 1.1)

    assert generation._inpaint_size_from_reference(1024, 768) is None


# --- edit_image top-level mask + prompt wrap ------------------------------


@pytest.mark.asyncio
async def test_edit_image_wraps_prompt_when_mask_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """edit_image() 在 mask 不为空时把 prompt 包成 invariant 模板，并把同一份 mask
    向下游 _dispatch_image 透传一次。"""
    seen: dict[str, Any] = {}

    async def fake_dispatch_image(**kwargs: Any):
        seen["prompt"] = kwargs["prompt"]
        seen["mask"] = kwargs["mask"]
        seen["images"] = kwargs["images"]
        for result in ():
            yield result

    monkeypatch.setattr(upstream, "_dispatch_image", fake_dispatch_image)

    img = _make_png()
    mask = _make_mask_png()

    async def _drain() -> None:
        async for _ in upstream.edit_image(
            prompt="add a red hat",
            size="1024x1024",
            images=[img],
            mask=mask,
        ):
            pass

    await _drain()

    assert seen["mask"] == mask
    assert seen["images"] == [img]
    # invariant 前缀稳定 → cache friendly。
    assert seen["prompt"].startswith("Inside the masked region, add a red hat")


@pytest.mark.asyncio
async def test_edit_image_does_not_wrap_prompt_when_mask_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """mask=None 时 prompt 原样发，保持现有 i2i 行为。"""
    seen: dict[str, Any] = {}

    async def fake_dispatch_image(**kwargs: Any):
        seen["prompt"] = kwargs["prompt"]
        seen["mask"] = kwargs["mask"]
        for result in ():
            yield result

    monkeypatch.setattr(upstream, "_dispatch_image", fake_dispatch_image)

    async def _drain() -> None:
        async for _ in upstream.edit_image(
            prompt="add a hat",
            size="1024x1024",
            images=[_make_png()],
        ):
            pass

    await _drain()

    assert seen["mask"] is None
    assert seen["prompt"] == "add a hat"


# --- Bug 1: mask 任务必须走 generations，禁用 dual_race / responses fallback ---


class _StubProvider:
    """最小 provider 替身，让 _run_image_once_for_provider 跑得通。"""
    def __init__(self, name: str = "stub", *, image_jobs_enabled: bool = True) -> None:
        self.name = name
        self.image_jobs_enabled = image_jobs_enabled


def _install_run_image_once_stubs(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, dict[str, Any]]:
    """把 _run_image_once_for_provider 下游的所有 dispatch 函数替换成
    "记录调用 + 返回 dummy 成品"的 stub，方便断言只有正确路径被命中。"""
    calls: dict[str, dict[str, Any]] = {}

    async def fake_image_job_failover(**kwargs: Any) -> tuple[str, str | None]:
        calls["image_job_with_failover"] = kwargs
        return ("aGVsbG8=", None)  # noqa: S106 - test fixture

    async def fake_direct_edit_failover(**kwargs: Any) -> tuple[str, str | None]:
        calls["direct_edit_with_failover"] = kwargs
        return ("aGVsbG8=", None)  # noqa: S106

    async def fake_dual_race_image_action(**kwargs: Any):
        calls["dual_race_image_action"] = kwargs
        for result in ():
            yield result

    async def fake_dual_race_image_jobs_action(**kwargs: Any):
        calls["dual_race_image_jobs_action"] = kwargs
        for result in ():
            yield result

    async def fake_race_responses_image(**kwargs: Any) -> tuple[str, str | None]:
        calls["race_responses_image"] = kwargs
        return ("aGVsbG8=", None)  # noqa: S106

    monkeypatch.setattr(upstream, "_image_job_with_failover", fake_image_job_failover)
    monkeypatch.setattr(upstream, "_direct_edit_image_with_failover", fake_direct_edit_failover)
    monkeypatch.setattr(upstream, "_dual_race_image_action", fake_dual_race_image_action)
    monkeypatch.setattr(upstream, "_dual_race_image_jobs_action", fake_dual_race_image_jobs_action)
    monkeypatch.setattr(upstream, "_race_responses_image", fake_race_responses_image)
    return calls


@pytest.mark.asyncio
async def test_mask_task_with_dual_race_engine_skips_responses_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 1：dual_race engine + mask → 不进 dual_race（responses lane 不带 mask 会
    静默退化）；走 image_job_with_failover 单 lane 锁 generations。"""
    calls = _install_run_image_once_stubs(monkeypatch)

    async def _drain() -> None:
        async for _ in upstream._run_image_once_for_provider(
            action="edit",
            provider=_StubProvider(image_jobs_enabled=True),
            channel=upstream._IMAGE_CHANNEL_AUTO,
            engine=upstream._IMAGE_ROUTE_DUAL_RACE,
            prompt="hi",
            size="1024x1024",
            images=[_make_png()],
            mask=_make_mask_png(),
            n=1,
            quality="high",
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=None,
        ):
            pass

    await _drain()

    assert "dual_race_image_action" not in calls
    assert "dual_race_image_jobs_action" not in calls
    assert "race_responses_image" not in calls
    assert "image_job_with_failover" in calls
    # 锁定 generations endpoint，禁止内部切到 responses
    assert calls["image_job_with_failover"]["endpoint_override"] == "generations"
    assert calls["image_job_with_failover"]["mask"] is not None


@pytest.mark.asyncio
async def test_mask_task_with_responses_engine_skips_responses_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 1：responses engine + mask 时也禁用 responses 路径，强制走 generations。"""
    calls = _install_run_image_once_stubs(monkeypatch)

    async def _drain() -> None:
        async for _ in upstream._run_image_once_for_provider(
            action="edit",
            provider=_StubProvider(image_jobs_enabled=True),
            channel=upstream._IMAGE_CHANNEL_AUTO,
            engine=upstream._IMAGE_ROUTE_RESPONSES,
            prompt="hi",
            size="1024x1024",
            images=[_make_png()],
            mask=_make_mask_png(),
            n=1,
            quality="high",
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=None,
        ):
            pass

    await _drain()

    assert "race_responses_image" not in calls
    assert "image_job_with_failover" in calls
    assert calls["image_job_with_failover"]["endpoint_override"] == "generations"


@pytest.mark.asyncio
async def test_mask_task_use_jobs_false_goes_direct_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 1：channel=stream_only（use_jobs=False）+ mask 走 direct edits，不走
    sidecar / dual_race / responses。"""
    calls = _install_run_image_once_stubs(monkeypatch)

    async def _drain() -> None:
        async for _ in upstream._run_image_once_for_provider(
            action="edit",
            provider=_StubProvider(image_jobs_enabled=False),
            channel=upstream._IMAGE_CHANNEL_STREAM_ONLY,
            engine=upstream._IMAGE_ROUTE_DUAL_RACE,
            prompt="hi",
            size="1024x1024",
            images=[_make_png()],
            mask=_make_mask_png(),
            n=1,
            quality="high",
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=None,
        ):
            pass

    await _drain()

    assert "image_job_with_failover" not in calls
    assert "dual_race_image_action" not in calls
    assert "race_responses_image" not in calls
    assert "direct_edit_with_failover" in calls
    assert calls["direct_edit_with_failover"]["mask"] is not None


@pytest.mark.asyncio
async def test_mask_with_generate_action_raises_invalid_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 1：mask + action=generate（不该发生）应该 terminal 报错而非走 race。"""
    _install_run_image_once_stubs(monkeypatch)

    async def _drain() -> None:
        async for _ in upstream._run_image_once_for_provider(
            action="generate",
            provider=_StubProvider(),
            channel=upstream._IMAGE_CHANNEL_AUTO,
            engine=upstream._IMAGE_ROUTE_DUAL_RACE,
            prompt="hi",
            size="1024x1024",
            images=None,
            mask=_make_mask_png(),
            n=1,
            quality="high",
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=None,
        ):
            pass

    with pytest.raises(upstream.UpstreamError) as ei:
        await _drain()
    assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_non_mask_task_dual_race_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非 mask 任务（mask=None）+ dual_race 仍走 _dual_race_image_jobs_action，
    确认 Bug 1 修复没破坏现有行为。"""
    calls = _install_run_image_once_stubs(monkeypatch)

    async def _drain() -> None:
        async for _ in upstream._run_image_once_for_provider(
            action="edit",
            provider=_StubProvider(image_jobs_enabled=True),
            channel=upstream._IMAGE_CHANNEL_AUTO,
            engine=upstream._IMAGE_ROUTE_DUAL_RACE,
            prompt="hi",
            size="1024x1024",
            images=[_make_png()],
            mask=None,
            n=1,
            quality="high",
            output_format=None,
            output_compression=None,
            background=None,
            moderation=None,
            model=None,
            progress_callback=None,
        ):
            pass

    await _drain()

    # mask=None 走原 dispatch（dual_race jobs lane）
    assert "dual_race_image_jobs_action" in calls
    assert "image_job_with_failover" not in calls
