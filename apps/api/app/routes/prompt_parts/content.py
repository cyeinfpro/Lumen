"""Video prompt-enhancement request models and content assembly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.models import Image, Video
from lumen_core.schemas import VideoReferenceMediaIn


class VideoEnhanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=10000)
    action: str = Field(default="t2v", max_length=32)
    model: str = Field(default="", max_length=128)
    duration_s: int | None = Field(default=None, ge=-1, le=60)
    resolution: str | None = Field(default=None, max_length=32)
    aspect_ratio: str | None = Field(default=None, max_length=32)
    generate_audio: bool | None = None
    input_image_id: str | None = Field(default=None, max_length=36)
    variant_count: int = Field(default=1, ge=1, le=3)
    reference_media: list[VideoReferenceMediaIn] = Field(
        default_factory=list,
        max_length=12,
    )

    @model_validator(mode="after")
    def require_prompt_or_reference(self) -> "VideoEnhanceIn":
        if (
            not self.text.strip()
            and not (self.input_image_id or "").strip()
            and not self.reference_media
        ):
            raise ValueError("text or reference media is required")
        return self


@dataclass(frozen=True)
class ContentRuntime:
    owned_image: Callable[..., Awaitable[Image]]
    owned_video: Callable[..., Awaitable[Video]]
    image_data_url: Callable[[Image], Awaitable[str | None]]
    video_poster_data_url: Callable[[Video], Awaitable[str | None]]
    resolve_public_base_url: Callable[[Request, AsyncSession], Awaitable[str | None]]
    video_reference_public_url: Callable[[Video, str], tuple[str, bool]]


@dataclass
class _ContentState:
    content: list[dict[str, Any]]
    media_payload_bytes: int = 0
    token_changed: bool = False
    public_base_url: str | None = None


def append_input_image_with_budget(
    content: list[dict[str, Any]],
    image_url: str,
    *,
    media_payload_bytes: int,
    media_total_max_bytes: int,
) -> tuple[bool, int]:
    next_payload_bytes = media_payload_bytes
    if image_url.startswith("data:image/"):
        payload_bytes = len(image_url.encode("utf-8"))
        if media_payload_bytes + payload_bytes > media_total_max_bytes:
            return False, media_payload_bytes
        next_payload_bytes += payload_bytes
    content.append({"type": "input_image", "image_url": image_url})
    return True, next_payload_bytes


def external_image_url_for_input(url: str | None) -> str | None:
    value = (url or "").strip()
    if value.startswith("data:image/"):
        return value
    if value.startswith(("http://", "https://")):
        return value
    return None


def append_video_context_line(lines: list[str], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        clean = value.strip()
        if not clean:
            return
        lines.append(f"{key}: {clean}")
        return
    lines.append(f"{key}: {value}")


def reference_anchor(ref_id: str | None, kind: str, index: int) -> str:
    clean = (ref_id or "").strip().lower()
    parts = clean.split(":")
    if (
        len(parts) == 3
        and parts[0] == "ref"
        and parts[1] == kind
        and parts[2].isdigit()
        and int(parts[2]) > 0
    ):
        return f"[{clean}]"
    return f"[ref:{kind}:{index}]"


def _base_prompt_lines(body: VideoEnhanceIn) -> list[str]:
    return [
        "任务：将下面的输入优化成可直接提交给视频生成模型的一段提示词。",
        (
            "优化重点：按火山/Seedance 视频提示词结构补齐主体动作、"
            "运动轨迹、运镜、首尾时间推进、视觉风格和参考一致性；"
            "不要生成字幕、水印、UI 文案、seed 或命令参数。"
        ),
        (
            "Vibe Creating 判断：先判断真实场景是否适合故事、情绪、记忆、"
            "氛围、意象或主观体验表达；再判断原文应直接放行、轻度提纯、"
            "直接改写、先补问、原样保留，还是只给可选 VC 版。"
        ),
        (
            "信息密度：若缺少视觉锚点、行为/状态、局部调性或视频主题/风格，"
            '请先输出 action="ask_first" 的 1-3 个必要问题，不要硬猜。'
        ),
        (
            "参考素材锚点合同：参考图/视频都有唯一锚点，例如 [ref:image:1]。"
            "优化后必须保留用户实际引用到的锚点；如果同类素材有多张而用户只说"
            "这张图、那张图、左图、右图等模糊指代，请输出 ask_first 补问，"
            "不要自行选择。"
        ),
        (
            "约束优先：用户明确写出的台词、旁白、音乐、音效、歌词、"
            "镜头结构、参数保留要求和交付格式必须保留；未要求保留的焦段、"
            "光圈、曝光、ISO、设备和纯剪辑参数可转译为自然观看感受。"
        ),
        f"原始描述：{body.text.strip() or '（未填写，请主要根据参考素材生成）'}",
    ]


def _append_request_context(lines: list[str], body: VideoEnhanceIn) -> None:
    append_video_context_line(lines, "生成模式", body.action)
    append_video_context_line(lines, "模型", body.model)
    append_video_context_line(lines, "时长", body.duration_s)
    append_video_context_line(lines, "分辨率", body.resolution)
    append_video_context_line(lines, "画幅", body.aspect_ratio)
    if body.generate_audio is not None:
        lines.append(f"音频：{'需要' if body.generate_audio else '不需要'}")
    if body.variant_count > 1:
        lines.append(
            f"候选方案数量：{body.variant_count}；必须按 "
            '<variant action="direct_rewrite" title="...">...</variant> 输出，'
            "第一项为推荐最佳；action 只能是 direct_pass、light_refine、"
            "direct_rewrite、ask_first、keep_original、optional_vc；"
            "信息不足或低适配时可只输出 1 个补问/保留候选，不要为了凑数硬改写；"
            "正常改写时每个方案应有不同侧重，分别强化动作轨迹、运镜/镜头语言、"
            "时间推进/节奏/连续性/参考素材一致性。"
        )


def _append_image(
    state: _ContentState,
    image_url: str,
    *,
    media_total_max_bytes: int,
    too_large_text: str,
) -> None:
    appended, state.media_payload_bytes = append_input_image_with_budget(
        state.content,
        image_url,
        media_payload_bytes=state.media_payload_bytes,
        media_total_max_bytes=media_total_max_bytes,
    )
    if not appended:
        state.content.append({"type": "input_text", "text": too_large_text})


async def _append_primary_image(
    state: _ContentState,
    body: VideoEnhanceIn,
    *,
    db: AsyncSession,
    user_id: str,
    runtime: ContentRuntime,
    media_total_max_bytes: int,
) -> None:
    if not body.input_image_id:
        return
    image = await runtime.owned_image(
        db,
        user_id=user_id,
        image_id=body.input_image_id,
    )
    state.content.append({"type": "input_text", "text": "首帧参考图："})
    image_url = await runtime.image_data_url(image)
    if image_url:
        _append_image(
            state,
            image_url,
            media_total_max_bytes=media_total_max_bytes,
            too_large_text=(
                f"首帧参考图 image_id={image.id}，"
                "但参考素材总体过大，已降级为文字引用。"
            ),
        )
        return
    state.content.append(
        {
            "type": "input_text",
            "text": f"首帧参考图 image_id={image.id}，但图片过大或暂不可读取。",
        }
    )


def _reference_label(item: VideoReferenceMediaIn, index: int) -> str:
    noun = (
        "图片" if item.kind == "image" else "音频" if item.kind == "audio" else "视频"
    )
    return (item.label or "").strip() or f"参考{noun} {index}"


def _reference_kind_index(
    reference_media: list[VideoReferenceMediaIn],
    index: int,
    kind: str,
) -> int:
    return sum(1 for prior in reference_media[:index] if prior.kind == kind)


async def _append_owned_image_reference(
    state: _ContentState,
    item: VideoReferenceMediaIn,
    *,
    label: str,
    anchor: str,
    db: AsyncSession,
    user_id: str,
    runtime: ContentRuntime,
    media_total_max_bytes: int,
) -> None:
    image = await runtime.owned_image(db, user_id=user_id, image_id=item.image_id)
    state.content.append(
        {
            "type": "input_text",
            "text": (f"{label} 锚点 {anchor}；优化输出引用该素材时必须保留此锚点："),
        }
    )
    image_url = await runtime.image_data_url(image)
    if image_url:
        _append_image(
            state,
            image_url,
            media_total_max_bytes=media_total_max_bytes,
            too_large_text=(
                f"{label} image_id={image.id}，但参考素材总体过大，已降级为文字引用。"
            ),
        )
        return
    state.content.append(
        {
            "type": "input_text",
            "text": f"{label} image_id={image.id}，但图片过大或暂不可读取。",
        }
    )


def _append_external_image_reference(
    state: _ContentState,
    item: VideoReferenceMediaIn,
    *,
    label: str,
    anchor: str,
    media_total_max_bytes: int,
) -> None:
    image_url = external_image_url_for_input(item.url)
    if not image_url:
        state.content.append(
            {
                "type": "input_text",
                "text": (
                    f"{label} 锚点 {anchor}；外部图片引用：{(item.url or '').strip()}"
                ),
            }
        )
        return
    is_data_image = image_url.startswith("data:image/")
    state.content.append(
        {
            "type": "input_text",
            "text": (
                f"{label} 锚点 {anchor}；外部图片数据 URL："
                if is_data_image
                else f"{label} 锚点 {anchor}；外部图片 URL：{image_url}"
            ),
        }
    )
    _append_image(
        state,
        image_url,
        media_total_max_bytes=media_total_max_bytes,
        too_large_text=(
            f"{label} 外部图片数据 URL 过大，已忽略图片内容，仅保留标签。"
            if is_data_image
            else f"{label} 外部图片过大，已降级为 URL 文字引用。"
        ),
    )


async def _append_image_reference(
    state: _ContentState,
    item: VideoReferenceMediaIn,
    *,
    label: str,
    anchor: str,
    db: AsyncSession,
    user_id: str,
    runtime: ContentRuntime,
    media_total_max_bytes: int,
) -> None:
    if item.image_id:
        await _append_owned_image_reference(
            state,
            item,
            label=label,
            anchor=anchor,
            db=db,
            user_id=user_id,
            runtime=runtime,
            media_total_max_bytes=media_total_max_bytes,
        )
        return
    _append_external_image_reference(
        state,
        item,
        label=label,
        anchor=anchor,
        media_total_max_bytes=media_total_max_bytes,
    )


async def _append_video_reference(
    state: _ContentState,
    item: VideoReferenceMediaIn,
    *,
    label: str,
    anchor: str,
    request: Request,
    db: AsyncSession,
    user_id: str,
    runtime: ContentRuntime,
    media_total_max_bytes: int,
) -> None:
    if not item.video_id:
        state.content.append(
            {
                "type": "input_text",
                "text": (
                    f"{label} 锚点 {anchor}；"
                    f"外部参考视频 URL：{(item.url or '').strip()}"
                ),
            }
        )
        return
    video = await runtime.owned_video(db, user_id=user_id, video_id=item.video_id)
    if state.public_base_url is None:
        state.public_base_url = await runtime.resolve_public_base_url(request, db)
    video_url = ""
    if state.public_base_url:
        video_url, changed = runtime.video_reference_public_url(
            video,
            state.public_base_url,
        )
        state.token_changed = state.token_changed or changed
    details = [
        f"{label}：参考视频",
        f"anchor={anchor}",
        f"video_id={video.id}",
        f"mime={video.mime}",
        f"duration_ms={video.duration_ms}",
        f"size_bytes={video.size_bytes}",
    ]
    if video_url:
        details.append(f"url={video_url}")
    state.content.append({"type": "input_text", "text": "；".join(details)})
    poster_url = await runtime.video_poster_data_url(video)
    if not poster_url:
        return
    state.content.append(
        {
            "type": "input_text",
            "text": f"{label} 的 poster / 首帧视觉参考：",
        }
    )
    _append_image(
        state,
        poster_url,
        media_total_max_bytes=media_total_max_bytes,
        too_large_text=f"{label} 的 poster 过大，已降级为文字引用。",
    )


async def _append_reference(
    state: _ContentState,
    body: VideoEnhanceIn,
    item: VideoReferenceMediaIn,
    index: int,
    *,
    request: Request,
    db: AsyncSession,
    user_id: str,
    runtime: ContentRuntime,
    media_total_max_bytes: int,
) -> None:
    label = _reference_label(item, index)
    same_kind_index = _reference_kind_index(body.reference_media, index, item.kind)
    anchor = reference_anchor(item.ref_id, item.kind, same_kind_index)
    if item.kind == "image":
        await _append_image_reference(
            state,
            item,
            label=label,
            anchor=anchor,
            db=db,
            user_id=user_id,
            runtime=runtime,
            media_total_max_bytes=media_total_max_bytes,
        )
        return
    if item.kind == "audio":
        state.content.append(
            {
                "type": "input_text",
                "text": (
                    f"{label} 锚点 {anchor}；"
                    f"外部参考音频 URL：{(item.url or '').strip()}"
                ),
            }
        )
        return
    await _append_video_reference(
        state,
        item,
        label=label,
        anchor=anchor,
        request=request,
        db=db,
        user_id=user_id,
        runtime=runtime,
        media_total_max_bytes=media_total_max_bytes,
    )


async def build_video_enhance_content(
    body: VideoEnhanceIn,
    *,
    request: Request,
    db: AsyncSession,
    user_id: str,
    runtime: ContentRuntime,
    media_total_max_bytes: int,
) -> tuple[list[dict[str, Any]], bool]:
    lines = _base_prompt_lines(body)
    _append_request_context(lines, body)
    state = _ContentState(content=[{"type": "input_text", "text": "\n".join(lines)}])
    await _append_primary_image(
        state,
        body,
        db=db,
        user_id=user_id,
        runtime=runtime,
        media_total_max_bytes=media_total_max_bytes,
    )
    for index, item in enumerate(body.reference_media, start=1):
        await _append_reference(
            state,
            body,
            item,
            index,
            request=request,
            db=db,
            user_id=user_id,
            runtime=runtime,
            media_total_max_bytes=media_total_max_bytes,
        )
    return state.content, state.token_changed
