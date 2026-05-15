"""Structured workflow routes.

The apparel model showcase workflow is a project-style layer on top of the
existing durable image/text task system. Endpoints here own stage state and
approvals; generations/completions still run through the same worker queues so
refreshing or closing the browser does not lose progress.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Iterable

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from PIL import Image as PILImage
from sqlalchemy import delete, desc, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from lumen_core.providers import (
    ProviderProxyDefinition,
    parse_proxy_json,
    resolve_provider_proxy_url,
)
from lumen_core.runtime_settings import get_spec

from lumen_core.constants import (
    CompletionStatus,
    GenerationStatus,
    ImageSource,
    ImageVisibility,
    Intent,
    Role,
)
from lumen_core.models import (
    Completion,
    Conversation,
    Generation,
    Image,
    ImageVariant,
    Message,
    ModelCandidate,
    ModelLibraryHiddenPreset,
    ModelLibraryItem,
    new_uuid7,
    OutboxEvent,
    PosterMaster,
    PosterRender,
    PosterStyleItem,
    QualityReport,
    User,
    WorkflowRun,
    WorkflowStep,
)
from lumen_core.model_image_metadata import (
    build_model_image_metadata,
    model_image_filename,
    parse_model_image_metadata,
)
from lumen_core.schemas import (
    AccessoryPlanIn,
    AccessoryPreviewCreateIn,
    AccessorySelectionIn,
    AgeSegment,
    ApparelModelLibraryAutoTagOut,
    ApparelModelLibraryBatchDeleteIn,
    ApparelModelLibraryBatchDeleteOut,
    ApparelModelLibraryGenerateIn,
    ApparelModelLibraryItemCreateIn,
    ApparelModelLibraryItemOut,
    ApparelModelLibraryItemPatchIn,
    ApparelModelLibraryJobItemOut,
    ApparelModelLibraryJobOut,
    ApparelModelLibraryJobsClearOut,
    ApparelModelLibraryJobsOut,
    ApparelModelLibraryListOut,
    ApparelModelLibrarySaveJobItemIn,
    ApparelModelLibrarySelectIn,
    ApparelModelLibrarySyncOut,
    ApparelModelLibrarySyncStateOut,
    ApparelWorkflowCreateIn,
    ApparelWorkflowCreateOut,
    ChatParamsIn,
    CopyAnalysisApproveIn,
    GenerationOut,
    ImageOut,
    ImageParamsIn,
    ImageRevisionIn,
    ModelCandidateApproveIn,
    ModelCandidateSaveToLibraryIn,
    ModelCandidatesCreateIn,
    ModelCandidateOut,
    PosterDesignWorkflowCreateIn,
    PosterDesignWorkflowCreateOut,
    PosterInpaintIn,
    PosterMasterApproveIn,
    PosterMasterOut,
    PosterMastersCreateIn,
    PosterRenderOut,
    PosterRendersCreateIn,
    PosterReviseIn,
    ProductAnalysisApproveIn,
    QualityReportOut,
    ShowcaseImagesCreateIn,
    WorkflowRunListItemOut,
    WorkflowRunListOut,
    WorkflowRunOut,
    WorkflowRunPatchIn,
    WorkflowStepOut,
)

from ..db import get_db
from ..deps import CurrentUser, verify_csrf
from ..config import settings
from ..redis_client import get_redis
from ..runtime_settings import get_setting
from .messages import (
    _create_assistant_task,
    _publish_assistant_task,
    _publish_message_appended,
)
from ._showcase_shot_pool import (
    SHOT_CLASS_ORDER,
    ShotClass,
    ShotPool,
    ShotVariant,
    age_soft_constraint as _age_soft_constraint,
    resolve_pool_band as _resolve_pool_band,
    select_variants as _select_shot_variants,
    shot_class_distribution as _shot_class_distribution,
)
from ._showcase_shot_pool_adult import ADULT_POOL
from ._showcase_shot_pool_kids import CHILD_POOL, TODDLER_POOL


SHOT_POOL_BY_BAND: dict[str, ShotPool] = {
    "young_adult": ADULT_POOL,
    "child": CHILD_POOL,
    "toddler": TODDLER_POOL,
}


router = APIRouter(prefix="/workflows", tags=["workflows"])
logger = logging.getLogger(__name__)


WORKFLOW_TYPE = "apparel_model_showcase"
WORKFLOW_STEPS = [
    "upload_product",
    "product_analysis",
    "model_settings",
    "model_candidates",
    "model_approval",
    "showcase_generation",
    "quality_review",
    "delivery",
]
MODEL_CANDIDATE_COUNT = 3
MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY = "model_library.sync_use_proxy_pool"
MODEL_LIBRARY_SYNC_PROXY_NAME_KEY = "model_library.sync_proxy_name"
DEFAULT_SHOT_PLAN = [
    "front_full_body",
    "natural_pose",
    "detail_half_body",
    "side_or_back",
]

MODEL_LIBRARY_ROOT_KEY = "apparel-model-library"
# apparel-model-library 常量 + 纯 helper 全部从 _apparel_library 导入。
# 这里 re-export 是为了让既有测试（apps/api/tests/test_workflows_route.py）
# 仍能通过 `workflows._normalize_age_segment` 等私有路径访问。
from app.routes._apparel_library import (  # noqa: E402, F401
    MODEL_LIBRARY_AGE_SEGMENTS,
    MODEL_LIBRARY_APPEARANCES,
    MODEL_LIBRARY_FETCH_TIMEOUT_SECONDS,
    MODEL_LIBRARY_FOLDER_BY_AGE,
    MODEL_LIBRARY_GENDER_SEGMENTS,
    MODEL_LIBRARY_GENERATE_COUNTS,
    MODEL_LIBRARY_GENERATE_STEP_KEY,
    MODEL_LIBRARY_GENERATE_WORKER_ACTION,
    MODEL_LIBRARY_IMAGE_SUFFIXES,
    MODEL_LIBRARY_MAX_BINARY_BYTES,
    MODEL_LIBRARY_SCHEMA_VERSION,
    MODEL_LIBRARY_SOURCES,
    MODEL_LIBRARY_SYNC_COOLDOWN_SECONDS,
    MODEL_LIBRARY_SYNC_MODES,
    MODEL_LIBRARY_SYNC_RETRY_COOLDOWN_SECONDS,
    WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
    _SYNC_LOCK,
    _age_segment_from_folder_name,
    _gender_from_folder_name,
    _library_item_url,
    _model_library_folder_for_age,
    _normalize_age_segment,
    _normalize_appearance,
    _normalize_model_gender,
    _preset_id_from_path,
    _title_from_preset_id,
)

HIDDEN_PROJECT_WORKFLOW_TYPES = frozenset(
    {
        WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
        "poster_style_library_generate",
    }
)

STEP_LABELS = {
    "upload_product": "上传商品",
    "product_analysis": "商品约束",
    "model_settings": "模特设定",
    "model_candidates": "模特候选",
    "model_approval": "方案确认",
    "showcase_generation": "商品融合",
    "quality_review": "质检返修",
    "delivery": "交付",
}

TEMPLATE_LABELS = {
    "white_ecommerce": "白底主图",
    "premium_studio": "高级棚拍",
    "urban_commute": "质感街拍",
    "lifestyle": "精品空间",
    "daily_snapshot": "日常随拍",
    "natural_phone_snapshot": "自然手机摄影",
    "social_seed": "自然种草",
}

SCENE_ENVIRONMENT_TEMPLATES = frozenset({
    "daily_snapshot",
    "natural_phone_snapshot",
    "social_seed",
})


def _scene_environment_outdoor_phrase(template: str, category: str) -> str:
    """3 个生活化模板的户外变体；indoor 走原 prompt 保持原有场景锚点。"""
    if template not in SCENE_ENVIRONMENT_TEMPLATES:
        return ""
    scenes = {
        "daily_snapshot": (
            f"户外随拍场景（街角、阳台、庭院、咖啡店外或公园），"
            f"自然日光为主，背景与{category}搭配但不杂乱，超真实、超自然、不像棚拍"
        ),
        "natural_phone_snapshot": (
            f"真实手机竖屏随手拍，平视或自然手持视角；"
            f"户外随手拍场景（街道、公园、海边、景点或建筑前），"
            f"氛围跟{category}搭配；自然日光带方向（侧光、逆光或斜上光）；"
            f"少量生活细节真实但不要遮挡{category}主体，不要棚拍或亚马逊主图感"
        ),
        "social_seed": (
            f"户外种草场景（街拍、橱窗、店外、景点或自然环境），"
            f"自然日光，与{category}匹配的松弛、真实、有生活感氛围"
        ),
    }
    return scenes.get(template, "")


def _template_requirement(
    template: str,
    product_analysis: dict[str, Any],
    scene_environment: str = "indoor",
) -> str:
    category = str(product_analysis.get("category") or "服饰").strip() or "服饰"
    recommended_background = str(product_analysis.get("background_recommendation") or "").strip()
    matched_background = (
        recommended_background
        if recommended_background and recommended_background.lower() != "unknown"
        else f"根据{category}选择好看的服饰商业摄影氛围"
    )
    phone_scene = (
        recommended_background
        if recommended_background and recommended_background.lower() != "unknown"
        else f"与{category}风格搭配的真实生活空间"
    )
    outdoor_phrase = (
        _scene_environment_outdoor_phrase(template, category)
        if scene_environment == "outdoor"
        else ""
    )
    requirements = {
        "white_ecommerce": "白底或近白底，柔和棚拍光",
        "premium_studio": f"{matched_background}，高级棚拍质感，柔和光影",
        "urban_commute": f"与{category}匹配的质感街拍氛围，真实自然但不杂乱",
        "lifestyle": f"与{category}匹配的精品空间氛围，克制、高级、有层次",
        "daily_snapshot": (
            outdoor_phrase
            or f"与{category}匹配的日常随拍质感，手机拍摄感，超真实、超自然、不像棚拍"
        ),
        "natural_phone_snapshot": (
            outdoor_phrase
            or (
                f"真实手机竖屏随手拍，平视或自然手持视角；"
                f"{phone_scene}，氛围跟{category}搭配；自然光或室内暖光；"
                f"少量生活细节真实但不要遮挡{category}主体，不要棚拍或亚马逊主图感"
            )
        ),
        "social_seed": (
            outdoor_phrase
            or f"与{category}匹配的自然种草氛围，松弛、真实、有生活感"
        ),
    }
    return requirements.get(template, TEMPLATE_LABELS.get(template, template))


# 每个模板的渲染指令都包含四段：
# 1) 调性短语（杂志大片 / 真实街拍 / 自然日常）
# 2) 皮肤质感正面约束（毛孔细纹、自然光泽、皮下血色）
# 3) 真人瑕疵（按调性递增：商业类轻、真实类重）— 仅靠"真实/不磨皮"模型仍渲染完美无瑕
# 4) 简短禁令（塑料感、过度磨皮、AI 美颜脸）
_RENDER_DIRECTIONS: dict[str, str] = {
    "white_ecommerce": (
        "干净高级商业摄影，柔和棚拍光，服装细节清晰；"
        "皮肤保留真实毛孔细纹和自然光泽，皮下血色自然，鼻翼T区有真实油光；"
        "毛孔深浅不均，面部非完美对称；"
        "不要塑料感、过度磨皮、AI美颜脸；无文字水印"
    ),
    "premium_studio": (
        "杂志大片质感，高级棚拍光带方向，颧骨鼻梁有清晰高光、下颌阴影分明；"
        "皮肤真实有毛孔和细纹，自然光泽和皮下血色，眼神有真实质感；"
        "眉毛和嘴唇细微不对称，眼角有真实细纹；"
        "不要塑料感、过度磨皮、AI网红脸"
    ),
    "urban_commute": (
        "真实街头摄影质感，户外日光带明确方向（侧光、逆光或斜上光），"
        "建筑阴影投射在脸或地面，颧骨鼻梁有真实高光，半边脸略阴影；"
        "皮肤保留毛孔细纹和真实阴影，鼻翼T区自然油光，碎发和飞絮真实；"
        "有轻微痘印或泛红，皮肤色不完全均匀；"
        "不要塑料感、过度磨皮、AI美颜脸、全脸均匀照明"
    ),
    "lifestyle": (
        "真实空间摄影质感，窗光或室内射灯带明确方向，"
        "半边脸略阴影、颧骨鼻梁有真实高光，背景明暗渐变；"
        "皮肤有真实毛孔细纹和自然光泽，皮下血色自然，碎发不刻意；"
        "皮肤色不完全均匀，左右脸细微差异；"
        "不要塑料感、过度磨皮、AI网红脸、全脸均匀照明"
    ),
    "daily_snapshot": (
        "真实日常随拍质感，家中窗光或灯光带方向（侧窗光或顶光），"
        "颧骨鼻梁有真实高光，鼻翼下方有真实阴影，背景有光线渐变；"
        "皮肤保留真实毛孔和细纹，自然光泽和皮下血色，碎发自然；"
        "有轻微痘印或鼻翼油光，毛孔深浅不均；"
        "不要塑料感、过度磨皮、AI美颜脸、全脸均匀照明"
    ),
    "natural_phone_snapshot": (
        "真实手机照片质感，自然窗光或柔和室内暖光从侧面打来，"
        "半边脸略阴影、颧骨鼻梁有真实高光、皮肤上有细微光斑；"
        "服装细节可见；真实阴影、真实皮肤毛孔和细纹、自然碎发、衣服真实褶皱；"
        "有轻微痘印或鼻翼黑头，面部非完美对称；"
        "不要棚拍感、过度磨皮、AI网红脸、社交媒体截图界面、全脸均匀照明"
    ),
    "social_seed": (
        "真实生活种草质感，窗光或室内灯光带方向，"
        "颧骨鼻梁有真实高光、半边脸略阴影、玻璃反光真实；"
        "服装搭配清晰；皮肤有真实毛孔细纹和自然光泽，皮下血色自然，碎发真实；"
        "有轻微痘印或皮肤色不均，眼角真实细纹；"
        "不要塑料感、过度磨皮、AI美颜脸、全脸均匀照明，不要画面中出现镜子或镜面"
    ),
}


_RENDER_DIRECTIONS_OUTDOOR: dict[str, str] = {
    "daily_snapshot": (
        "真实日常随拍质感，户外自然日光带明确方向（侧光、逆光或斜上光），"
        "颧骨鼻梁有真实高光，半边脸略阴影，背景有真实空间深度；"
        "皮肤保留真实毛孔和细纹，自然光泽和皮下血色，碎发自然；"
        "有轻微痘印或鼻翼油光，毛孔深浅不均；"
        "不要塑料感、过度磨皮、AI美颜脸、全脸均匀照明"
    ),
    "natural_phone_snapshot": (
        "真实手机照片质感，户外自然日光从侧面或斜上方打来，"
        "半边脸略阴影、颧骨鼻梁有真实高光、皮肤上有细微光斑；"
        "服装细节可见；真实阴影、真实皮肤毛孔和细纹、自然碎发、衣服真实褶皱；"
        "有轻微痘印或鼻翼黑头，面部非完美对称；"
        "不要棚拍感、过度磨皮、AI网红脸、社交媒体截图界面、全脸均匀照明"
    ),
    "social_seed": (
        "真实生活种草质感，户外自然日光带方向（侧光、逆光或斜上光），"
        "颧骨鼻梁有真实高光、半边脸略阴影、建筑或绿植真实阴影投射；"
        "服装搭配清晰；皮肤有真实毛孔细纹和自然光泽，皮下血色自然，碎发真实；"
        "有轻微痘印或皮肤色不均，眼角真实细纹；"
        "不要塑料感、过度磨皮、AI美颜脸、全脸均匀照明，不要画面中出现镜子或镜面"
    ),
}


def _showcase_render_direction(template: str, scene_environment: str = "indoor") -> str:
    if (
        scene_environment == "outdoor"
        and template in SCENE_ENVIRONMENT_TEMPLATES
        and template in _RENDER_DIRECTIONS_OUTDOOR
    ):
        return _RENDER_DIRECTIONS_OUTDOOR[template]
    return _RENDER_DIRECTIONS.get(
        template,
        "真实摄影质感；皮肤保留真实毛孔细纹和自然光泽；不要塑料感、过度磨皮、AI美颜脸",
    )


_POSE_DIRECTIONS: dict[str, str] = {
    "white_ecommerce": "目录摄影舒展自然站姿，肩颈松弛、重心稳定，不僵硬",
    "premium_studio": "高级棚拍但动作克制，戏剧化只在光线和眼神，不靠夸张肢体",
    "urban_commute": "真实街头抓拍感，小幅动作像无意被定格，不刻意摆拍",
    "lifestyle": "从容松弛的空间感，小幅动作，有呼吸感",
    "daily_snapshot": "朋友视角随手拍，动作自然不刻意，身体重心可信",
    "natural_phone_snapshot": "姿态自然松弛，平视手持视角，小幅自然动作，正常手机透视",
    "social_seed": "自然穿搭分享感，小幅互动展示，不摆硬 pose",
}


def _showcase_pose_direction(template: str) -> str:
    return _POSE_DIRECTIONS.get(template, "姿态自然舒展")


_LIFESTYLE_TEMPLATES = frozenset({
    "urban_commute",
    "lifestyle",
    "daily_snapshot",
    "natural_phone_snapshot",
    "social_seed",
})


def _showcase_composition_direction(template: str) -> str:
    """生活化模板加构图变化，白底/棚拍保持中心构图不动。"""
    if template not in _LIFESTYLE_TEMPLATES:
        return ""
    return (
        "三分法构图，模特竖向落在画面左或右 1/3 线，对侧留呼吸感；"
        "可有轻微前景或景深层次，但不抢戏"
    )


_SQUARE_OR_LANDSCAPE_RATIOS = frozenset({"1:1", "4:3", "3:2", "16:9", "21:9"})


def _showcase_framing_direction(
    shot_class: str,
    framing: str | None,
    aspect_ratio: str = "4:5",
) -> str:
    """按机位类 + framing tag + 画面比例控制构图留白，避免身体被画面比例挤扁或拉长。

    - detail_half_body 是上半身近景，单独走"半身入镜"分支
    - tone_first 走"环境为主体、画面留白"
    - product_first（除 detail）走"全身入镜 + 上下留边"
    - 画面方/横时下调人物占比，横向多留余白，避免 AI 为了填高度而压缩身体
    """
    if shot_class == "detail_half_body":
        return (
            "上半身或胸口以上入镜，头顶留出适度边距，"
            "肩部肘部不顶画面边缘，背景留白干净"
        )
    is_square_or_landscape = aspect_ratio in _SQUARE_OR_LANDSCAPE_RATIOS
    if framing == "tone_first":
        if is_square_or_landscape:
            return (
                "人物占画面 45-60% 高度，左右留出充足余白，"
                "环境只作为氛围辅助，不要让背景压过服装主体，"
                "头脚完整且透视自然，不要为了填高度压扁身体"
            )
        return (
            "人物占画面 55-70% 高度，环境只作为氛围辅助，"
            "不要让背景压过服装主体，头脚完整且透视自然"
        )
    if is_square_or_landscape:
        return (
            "全身完整入镜，头顶上方留出 5-10% 边距，脚下完整不切断，"
            "人物占画面 60-75% 高度，左右留宽边，"
            "不要为了塞满画面而压缩或拉长身体比例"
        )
    return (
        "全身完整入镜，头顶上方留出 5-10% 边距，脚下完整不切断，"
        "人物占画面 70-85% 高度，避免顶满"
    )


def _showcase_prompt_brief(
    *,
    user_direction: str,
    template_direction: str,
    product_preserve: str,
    accessory_direction: str,
    model_consistency: str,
    shot_direction: str,
    pose_direction: str,
    framing_direction: str,
    quality_direction: str,
    render_direction: str,
    style_region: str,
) -> str:
    direction = template_direction.strip() or "背景与衣服图片搭配"
    extra_direction = _compact_showcase_user_direction(user_direction, style_region)
    if extra_direction:
        direction = f"{direction}；{extra_direction}"
    return "\n".join(
        [
            "请根据这张白底产品图和模特图，生成真实自然的真人模特穿搭图。",
            "",
            f"【商品 1:1 还原（最高优先级）】衣服以白底产品图为唯一来源，模特图只用于复刻人物身份。"
            f"重点保留：{product_preserve}，每一项必须清晰可见。"
            "不要改款、改色、改廓形、改领口袖型衣长、改图案/logo/印花/文字、改纽扣拉链口袋缝线拼接。",
            "",
            "要求：",
            "1. 摄影执行：真实模特目录摄影，约 50mm 标准焦段（不要广角拉头身比、不要长焦压扁），平视或胸口高度机位；身体重心可信，动作幅度小，避免跳跃、转圈、跪趴、后仰、大幅甩头。",
            "2. 模特按产品图自然穿着这件衣服，版型贴合，褶皱合理；不要人偶感、不要时装秀台步。",
            f"3. 模特参考模特图，{model_consistency}身材和表情自然。",
            f"4. 配饰：{accessory_direction}（不得遮挡商品）。",
            f"5. 场景：背景与衣服风格搭配，{direction}；画面里不要出现镜子或镜面反射。",
            f"6. 画质：{quality_direction}，{render_direction}。",
            f"7. 构图：{style_region}风格，{pose_direction}，{shot_direction}。",
            f"8. 画面：{framing_direction}；服装主体清晰可见。",
            "9. 单人照。",
        ]
    )


def _infer_age(text: str) -> int | None:
    lowered = (text or "").lower()
    age_match = re.search(r"(\d{1,2})\s*(?:岁|-year-old|year old|yo)", lowered)
    if age_match:
        try:
            return int(age_match.group(1))
        except ValueError:
            return None
    return None


def _infer_model_height_cm(text: str) -> int:
    age = _infer_age(text)
    if age is None:
        lowered = (text or "").lower()
        if any(word in lowered for word in ("儿童", "童装", "小朋友", "孩子", "kid", "kids", "child")):
            return 128
        return 168
    if age <= 2:
        return 90
    if age <= 12:
        return 80 + age * 6
    if age <= 17:
        return min(168, 128 + (age - 12) * 7)
    return 168


def _height_requirement(text: str) -> str:
    height_cm = _infer_model_height_cm(text)
    return (
        f"Keep the model's perceived height around {height_cm}cm, with consistent "
        "head-to-body ratio, limb length, and scale across all reference views and later images."
    )


def _age_direction(text: str) -> str:
    lowered = (text or "").lower()
    age = _infer_age(text)
    if age is not None and age <= 12 or any(
        word in lowered for word in ("儿童", "童装", "小朋友", "孩子", "kid", "kids", "child")
    ):
        age_text = f"around {age} years old" if age is not None else "child age range"
        return (
            f"The model should be {age_text}, with age-appropriate body proportion, face, "
            "posture, expression, and styling. Keep the look natural and non-adultized."
        )
    if age is not None and age < 18:
        return (
            f"The model should be around {age} years old, with teen-appropriate proportions, "
            "expression, pose, and styling."
        )
    if age is not None:
        return (
            f"The model should look around {age} years old, with expression, pose, styling, "
            "and scene adjusted to that age."
        )
    return (
        "Follow the user's requested model type, age impression, expression, pose, and styling; "
        "if unspecified, choose a natural ecommerce model that fits the garment."
    )


def _accessory_age_direction(text: str) -> str:
    lowered = (text or "").lower()
    age = _infer_age(text)
    if age is not None and age <= 12 or any(
        word in lowered for word in ("儿童", "童装", "小朋友", "孩子", "kid", "kids", "child")
    ):
        return (
            "Accessory styling must be child-appropriate: simple, safe-looking, playful but restrained, "
            "with no adult jewelry styling, glamour accessories, mature handbags, heels, or adult fashion cues."
        )
    if age is not None and age < 18 or any(word in lowered for word in ("青少年", "teen", "teenager")):
        return (
            "Accessory styling must fit a teenager: casual, age-appropriate, not childish, and not adult glamour."
        )
    if age is not None:
        return (
            f"Accessory styling must match an adult around {age} years old: commercially polished, natural, "
            "and appropriate for that age and product category."
        )
    return (
        "Infer the target age from the user's direction and product context, then choose accessories that match "
        "that age group instead of assuming the model is always a child."
    )


def _accessory_strength_direction(strength: str) -> str:
    if strength == "strong":
        return "更明显但仍克制，必须服务整体造型，不要压过模特身份和后续服装主体"
    if strength == "medium":
        return "中等存在感，清楚可见但不要主导画面"
    return "低存在感，近看可辨认，远看不抢主体"


_FACE_ARCHETYPES_FEMALE: tuple[str, ...] = (
    "oval face, almond eyes, straight nose, full lips, long straight hair, slim build",
    "round face, narrow long eyes, small upturned nose, subtle lips, short bob, soft standard build",
    "heart-shaped face, wide round eyes, petite nose, plump lips, medium wavy hair, tall slim build",
    "long oval face, sharp upturned eyes, high-bridge nose, balanced lips, long straight hair with highlights, tall lean build",
    "square face with strong jaw, deep-set eyes, straight nose, fuller lips, short curly hair, athletic build",
    "soft round face, relaxed almond eyes, rounded nose, subtle lips, long straight side-part hair, softly curvy build",
    "diamond face, monolid sharp eyes, narrow nose, thin lips, low ponytail, lean dancer-like build",
    "oblong face, double-eyelid almond eyes, medium straight nose, natural lips, shoulder-length wavy hair, willowy build",
)


_FACE_ARCHETYPES_MALE: tuple[str, ...] = (
    "oval face, calm double-eyelid eyes, straight nose, balanced lips, short side-part hair, lean tall build",
    "square face with strong jaw, sharp focused eyes, high-bridge nose, firm lips, short crew cut, broad athletic build",
    "long oval face, deep-set eyes, slim straight nose, neutral lips, medium-length wavy hair, tall slender build",
    "round face, friendly bright eyes, rounded nose, fuller lips, short messy textured hair, standard build",
    "diamond face, monolid eyes, narrow nose, thin lips, slicked-back hair, lean editorial build",
    "rectangular face, focused upturned eyes, defined nose, balanced lips, short undercut, fit toned build",
    "heart-shaped face, almond eyes, petite nose, soft lips, ear-length tousled hair, slim build",
    "oblong face, deep almond eyes, medium straight nose, natural lips, short side-part hair, tall lean build",
)


def _infer_candidate_gender(style_prompt: str, product_analysis: dict[str, Any]) -> str:
    """从风格描述 + 商品分类粗判性别；找不到信号就默认 female。

    英文只匹配独立词，避免 female 之类的词误触发 male。
    """
    text = " ".join(
        [style_prompt or "", str(product_analysis.get("category") or "")]
    ).lower()
    if any(
        token in text
        for token in ("女装", "女性", "女士", "女生", "女童")
    ) or any(
        re.search(pattern, text)
        for pattern in (
            r"\bfemale\b",
            r"\bwomen\b",
            r"\bwoman\b",
            r"\bgirl\b",
            r"\bwomenswear\b",
        )
    ):
        return "female"
    if any(
        token in text for token in ("男装", "男性", "男士", "男生", "男童")
    ) or any(
        re.search(pattern, text)
        for pattern in (
            r"\bmale\b",
            r"\bmen\b",
            r"\bman\b",
            r"\bboy\b",
            r"\bmenswear\b",
        )
    ):
        return "male"
    return "female"


def _model_diversity_anchor(
    *,
    candidate_index: int,
    gender: str | None,
    age_segment: str | None = None,
) -> str:
    """按 candidate_index 取一组差异化外貌锚点，避免多张候选收敛到同一张 AI 通用脸。

    toddler / child 用引导句，避免成人 archetype（如 ponytail、dancer build）套小孩的违和感。
    """
    if age_segment in {"toddler", "child"}:
        return (
            "Make this candidate visibly different from other candidates "
            "in face shape, hair length, and body type."
        )
    pool = (
        _FACE_ARCHETYPES_MALE
        if (gender or "").lower() == "male"
        else _FACE_ARCHETYPES_FEMALE
    )
    archetype = pool[(max(candidate_index, 1) - 1) % len(pool)]
    return (
        f"Look anchor for this candidate: {archetype}. "
        "Stay visibly distinct from other candidates; "
        "hair color and skin tone should follow the appearance direction."
    )


def _style_region_from_text(text: str) -> str:
    if any(token in text for token in ("东亚", "亚洲", "日系", "韩系", "中式")):
        return "亚洲"
    for region in ("欧美", "亚洲", "拉美", "中东", "非洲"):
        if region in text:
            return region
    return "自然商业摄影"


def _compact_showcase_user_direction(text: str, style_region: str) -> str:
    direction = (text or "").strip()
    if not direction:
        return ""
    for token in (
        f"外貌方向：{style_region}",
        style_region,
        "模特姿势生动活泼有活力",
        "姿势生动活泼有活力",
        "生动活泼有活力",
        "全身照",
        "自然走动回头",
        "走动回头",
        "自然走动",
        "走动",
        "回头",
    ):
        direction = direction.replace(token, "")
    direction = re.sub(r"[，,、；;\s]+", "，", direction).strip("，,、；; ")
    return direction[:60]


class _PublishBundle:
    def __init__(
        self,
        *,
        assistant_msg_id: str,
        message_ids: list[str],
        outbox_payloads: list[dict[str, Any]],
        outbox_rows: list[OutboxEvent],
    ) -> None:
        self.assistant_msg_id = assistant_msg_id
        self.message_ids = message_ids
        self.outbox_payloads = outbox_payloads
        self.outbox_rows = outbox_rows


def _http(code: str, msg: str, http: int = 400, **extra: Any) -> HTTPException:
    err: dict[str, Any] = {"code": code, "message": msg}
    if extra:
        err["details"] = extra
    return HTTPException(status_code=http, detail={"error": err})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dedupe_nonempty(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        v = value.strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _clean_optional_text(value: str | None, *, max_len: int = 120) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def _clean_style_tags(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        tag = raw.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag[:32])
        if len(out) >= 12:
            break
    return out


def _clean_string_list(values: Iterable[str], *, max_items: int, max_len: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        item = raw.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item[:max_len])
        if len(out) >= max_items:
            break
    return out


def _safe_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_now() -> str:
    return _now().isoformat().replace("+00:00", "Z")


def _storage_root() -> Path:
    return Path(settings.storage_root).resolve()


def _storage_path(storage_key: str) -> Path:
    root = _storage_root()
    if not storage_key or "\x00" in storage_key:
        raise _http("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise _http("invalid_path", "absolute storage paths are not allowed", 400)
    path = (root / key_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise _http("invalid_path", "storage path escapes root", 400)
    return path


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _fsync_dir(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json_file(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(default)
    except (OSError, json.JSONDecodeError) as exc:
        raise _http("invalid_index", f"invalid model library index: {path.name}", 500) from exc
    if not isinstance(data, dict):
        raise _http("invalid_index", f"invalid model library index: {path.name}", 500)
    return data


def _library_root() -> Path:
    return _storage_path(MODEL_LIBRARY_ROOT_KEY)


def _library_index_path() -> Path:
    return _library_root() / "index.json"


def _library_sync_state_path() -> Path:
    return _library_root() / "sync-state.json"


def _library_user_index_path(user_id: str) -> Path:
    return _library_root() / "users" / user_id / "index.json"


def _default_library_index() -> dict[str, Any]:
    return {
        "schema_version": MODEL_LIBRARY_SCHEMA_VERSION,
        "updated_at": None,
        "preset_items": [],
    }


def _default_user_library_index() -> dict[str, Any]:
    return {
        "schema_version": MODEL_LIBRARY_SCHEMA_VERSION,
        "updated_at": None,
        "hidden_preset_ids": [],
        "items": [],
    }


def _default_sync_state() -> dict[str, Any]:
    return {
        "schema_version": MODEL_LIBRARY_SCHEMA_VERSION,
        "last_success_at": None,
        "last_error": None,
        "last_attempt_at": None,
        "last_result": None,
    }


def _github_contents_url() -> str:
    return settings.apparel_model_library_github_contents_url.strip()


def _sync_mode() -> str:
    mode = settings.apparel_model_library_sync_mode.strip().lower()
    return mode if mode in MODEL_LIBRARY_SYNC_MODES else "admin_only"


def _model_library_http_client_kwargs(proxy_url: str | None = None) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(MODEL_LIBRARY_FETCH_TIMEOUT_SECONDS),
    }
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


async def _resolve_model_library_sync_proxy(
    db: AsyncSession,
) -> tuple[ProviderProxyDefinition | None, str | None]:
    use_spec = get_spec(MODEL_LIBRARY_SYNC_USE_PROXY_POOL_KEY)
    use_raw = await get_setting(db, use_spec) if use_spec is not None else None
    if str(use_raw or "0").strip() != "1":
        return None, None

    providers_spec = get_spec("providers")
    raw_providers = (
        await get_setting(db, providers_spec) if providers_spec is not None else None
    )
    proxies, errors = parse_proxy_json(raw_providers)
    for err in errors:
        logger.warning("model library sync proxy config warning: %s", err)

    enabled = [proxy for proxy in proxies if proxy.enabled]
    if not enabled:
        raise _http(
            "proxy_unavailable",
            "model library sync proxy pool is enabled but has no enabled proxies",
            409,
        )

    name_spec = get_spec(MODEL_LIBRARY_SYNC_PROXY_NAME_KEY)
    name_raw = await get_setting(db, name_spec) if name_spec is not None else None
    target_name = str(name_raw or "").strip()
    if target_name:
        proxy = next((p for p in enabled if p.name == target_name), None)
        if proxy is None:
            raise _http(
                "proxy_not_found",
                f"model library sync proxy '{target_name}' not found or disabled",
                409,
            )
    else:
        proxy = enabled[0]

    proxy_url = await resolve_provider_proxy_url(proxy)
    if not proxy_url:
        raise _http(
            "proxy_resolve_failed",
            f"model library sync proxy '{proxy.name}' could not be resolved",
            409,
        )
    return proxy, proxy_url


def _can_sync_library(user: User) -> bool:
    mode = _sync_mode()
    if mode == "disabled":
        return False
    if mode == "any_authenticated":
        return True
    return user.role == "admin"


def _sync_state_out(user: User) -> ApparelModelLibrarySyncStateOut:
    state = _read_json_file(_library_sync_state_path(), _default_sync_state())
    return ApparelModelLibrarySyncStateOut(
        last_success_at=_safe_datetime(state.get("last_success_at")),
        last_error=_clean_optional_text(state.get("last_error"), max_len=1000),
        can_sync=_can_sync_library(user),
        github_contents_url=_github_contents_url() or None,
    )


def _model_library_item_out(raw: dict[str, Any]) -> ApparelModelLibraryItemOut:
    item_id = str(raw.get("id") or "").strip()
    source = str(raw.get("source") or "").strip()
    if source not in {"preset", "favorite", "user_upload", "generated"}:
        source = "user_upload"
    image_id = _clean_optional_text(raw.get("image_id"), max_len=64)
    image_url = (
        f"/api/images/{image_id}/binary"
        if image_id
        else _library_item_url(item_id, "binary")
    )
    # user item 走 display2048 variant（按需 materialize）；preset 没有独立
    # display 变体，回落到 binary 原图。lightbox / 大图预览走这个。
    display_url = (
        f"/api/images/{image_id}/variants/display2048"
        if image_id
        else _library_item_url(item_id, "binary")
    )
    # 卡片小封面用：user item 复用 display2048（thumb256 variant 不一定生成
    # 且 endpoint 不按需 materialize，回落到 display2048 较稳）；preset 自带
    # 真小 thumb 文件。
    thumb_url = (
        f"/api/images/{image_id}/variants/display2048"
        if image_id
        else _library_item_url(item_id, "thumb")
    )
    created_at = _safe_datetime(raw.get("created_at")) or _safe_datetime(raw.get("updated_at")) or _now()
    visibility_scope = "global_preset" if source == "preset" else "user_private"
    style_tags = _clean_style_tags(raw.get("style_tags") or raw.get("tags") or [])
    gender = _clean_optional_text(raw.get("gender"), max_len=40)
    age_segment = _normalize_age_segment(raw.get("age_segment"))
    appearance_direction = _clean_optional_text(
        raw.get("appearance_direction"), max_len=80
    )
    metadata_filename = None
    metadata = raw.get("metadata_jsonb")
    if isinstance(metadata, dict):
        metadata_filename = _clean_optional_text(
            metadata.get("suggested_filename"), max_len=160
        )
    if not metadata_filename and image_id:
        image_metadata = raw.get("image_metadata_jsonb")
        if isinstance(image_metadata, dict):
            metadata_filename = _clean_optional_text(
                image_metadata.get("suggested_filename"), max_len=160
            )
    return ApparelModelLibraryItemOut(
        id=item_id,
        source=source,  # type: ignore[arg-type]
        visibility_scope=visibility_scope,  # type: ignore[arg-type]
        title=str(raw.get("title") or "未命名模特").strip()[:120],
        age_segment=age_segment,  # type: ignore[arg-type]
        gender=gender,
        appearance_direction=appearance_direction,
        style_tags=style_tags,
        image_url=image_url,
        display_url=display_url,
        thumb_url=thumb_url,
        image_id=image_id,
        preset_id=_clean_optional_text(raw.get("preset_id"), max_len=160),
        version=raw.get("version") if isinstance(raw.get("version"), int) else None,
        library_folder=_clean_optional_text(
            raw.get("library_folder")
            or _model_library_folder_for_age(raw.get("age_segment"), raw.get("gender")),
            max_len=40,
        ),
        prompt_hint=_clean_optional_text(raw.get("prompt_hint"), max_len=300),
        download_filename=metadata_filename
        or _model_library_download_filename(
            image_id=image_id or item_id,
            mime=None,
            age_segment=age_segment,
            gender=gender,
            appearance_direction=appearance_direction,
            style_tags=style_tags,
        ),
        created_at=created_at,
        updated_at=_safe_datetime(raw.get("updated_at")),
    )


def _load_global_library_index() -> dict[str, Any]:
    return _read_json_file(_library_index_path(), _default_library_index())


def _load_user_library_index(user_id: str) -> dict[str, Any]:
    """Read the legacy per-user JSON index.

    Kept for cutover safety: routes call ``_ensure_legacy_user_library_migrated``
    before DB reads so users do not lose visibility of old saved models when
    the new tables exist but the one-off backfill has not been run yet.
    """
    return _read_json_file(_library_user_index_path(user_id), _default_user_library_index())


def _save_global_library_index(index: dict[str, Any]) -> None:
    index["schema_version"] = MODEL_LIBRARY_SCHEMA_VERSION
    index["updated_at"] = _iso_now()
    _write_json_atomic(_library_index_path(), index)


def _save_user_library_index(user_id: str, index: dict[str, Any]) -> None:
    """Legacy file writer kept for migration tests and deletion tombstoning.

    Creation/update routes write through ORM; delete still updates this file
    so lazy migration cannot re-create rows the user already removed.
    """
    index["schema_version"] = MODEL_LIBRARY_SCHEMA_VERSION
    index["updated_at"] = _iso_now()
    _write_json_atomic(_library_user_index_path(user_id), index)


def _remove_user_library_item_from_legacy_index(user_id: str, item_id: str) -> bool:
    """Keep lazy JSON migration from resurrecting a DB-deleted user item."""
    index_path = _library_user_index_path(user_id)
    if not index_path.is_file():
        return False
    index = _load_user_library_index(user_id)
    raw_items = index.get("items")
    if not isinstance(raw_items, list):
        return False
    next_items: list[Any] = []
    removed = False
    for raw in raw_items:
        raw_id = str(raw.get("id") or "").strip() if isinstance(raw, dict) else ""
        if raw_id == item_id:
            removed = True
            continue
        next_items.append(raw)
    if not removed:
        return False
    index["items"] = next_items
    _save_user_library_index(user_id, index)
    return True


def _hide_preset_in_legacy_user_library_index(user_id: str, preset_id: str) -> bool:
    """Mirror preset hides into the legacy index while lazy migration exists."""
    index_path = _library_user_index_path(user_id)
    if not index_path.is_file():
        return False
    index = _load_user_library_index(user_id)
    hidden_ids = _dedupe_nonempty(index.get("hidden_preset_ids") or [])
    if preset_id in hidden_ids:
        return False
    index["hidden_preset_ids"] = [*hidden_ids, preset_id]
    _save_user_library_index(user_id, index)
    return True


def _save_sync_state(state: dict[str, Any]) -> None:
    state["schema_version"] = MODEL_LIBRARY_SCHEMA_VERSION
    _write_json_atomic(_library_sync_state_path(), state)


def _model_library_row_to_dict(row: ModelLibraryItem) -> dict[str, Any]:
    """Adapter so DB rows feed ``_model_library_item_out`` unchanged."""
    return {
        "id": row.id,
        "source": row.source,
        "image_id": row.image_id,
        "title": row.title,
        "age_segment": row.age_segment,
        "gender": row.gender,
        "appearance_direction": row.appearance_direction,
        "style_tags": list(row.style_tags or []),
        "library_folder": row.library_folder,
        "prompt_hint": row.prompt_hint,
        "auto_tagged_at": row.auto_tagged_at.isoformat() if row.auto_tagged_at else None,
        "auto_tag_notes": row.auto_tag_notes,
        "metadata_jsonb": dict(row.metadata_jsonb or {}),
        "owner_user_id": row.user_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _legacy_library_item_insert_values(
    *,
    user_id: str,
    raw: dict[str, Any],
    valid_image_ids: set[str],
) -> dict[str, Any] | None:
    item_id = str(raw.get("id") or "").strip()
    image_id = str(raw.get("image_id") or "").strip()
    if not item_id or not image_id or image_id not in valid_image_ids:
        return None
    source = str(raw.get("source") or "user_upload").strip()
    if source not in {"favorite", "user_upload", "generated"}:
        source = "user_upload"
    normalized_age = _normalize_age_segment(raw.get("age_segment"))
    normalized_gender = _normalize_model_gender(raw.get("gender"))
    created_at = (
        _safe_datetime(raw.get("created_at") if isinstance(raw.get("created_at"), str) else None)
        or _now()
    )
    updated_at = (
        _safe_datetime(raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None)
        or created_at
    )
    known_keys = {
        "id",
        "user_id",
        "owner_user_id",
        "source",
        "image_id",
        "title",
        "age_segment",
        "gender",
        "appearance_direction",
        "style_tags",
        "tags",
        "library_folder",
        "prompt_hint",
        "auto_tagged_at",
        "auto_tag_notes",
        "created_at",
        "updated_at",
    }
    return {
        "id": item_id,
        "user_id": user_id,
        "source": source,
        "image_id": image_id,
        "title": str(raw.get("title") or "").strip()[:120],
        "age_segment": normalized_age,
        "gender": normalized_gender,
        "appearance_direction": _clean_optional_text(
            raw.get("appearance_direction"), max_len=80
        ),
        "style_tags": _clean_style_tags(raw.get("style_tags") or raw.get("tags") or []),
        "library_folder": _clean_optional_text(
            raw.get("library_folder")
            or _model_library_folder_for_age(normalized_age, normalized_gender),
            max_len=64,
        ),
        "prompt_hint": _clean_optional_text(raw.get("prompt_hint"), max_len=1000),
        "auto_tagged_at": _safe_datetime(
            raw.get("auto_tagged_at") if isinstance(raw.get("auto_tagged_at"), str) else None
        ),
        "auto_tag_notes": _clean_optional_text(raw.get("auto_tag_notes"), max_len=200),
        "metadata_jsonb": {k: v for k, v in raw.items() if k not in known_keys},
        "created_at": created_at,
        "updated_at": updated_at,
    }


async def _ensure_legacy_user_library_migrated(
    db: AsyncSession, user_id: str
) -> bool:
    """Lazily backfill one user's legacy JSON index into PostgreSQL.

    The schema migration creates empty tables; deployments may not run the
    one-off script immediately. This guard keeps old saved models visible and
    functional by migrating valid rows on first access. It flushes, but leaves
    commit ownership to the route that called it.
    """
    index_path = _library_user_index_path(user_id)
    if not index_path.is_file():
        return False
    index = _load_user_library_index(user_id)
    raw_items = [
        item for item in (index.get("items") or []) if isinstance(item, dict)
    ]
    raw_hidden_ids = _dedupe_nonempty(index.get("hidden_preset_ids") or [])
    if not raw_items and not raw_hidden_ids:
        return False

    migrated = False
    item_ids = _dedupe_nonempty(str(item.get("id") or "") for item in raw_items)
    existing_item_ids: set[str] = set()
    if item_ids:
        rows = await db.execute(
            select(ModelLibraryItem.id).where(ModelLibraryItem.id.in_(item_ids))
        )
        existing_item_ids = set(rows.scalars().all())

    image_ids = _dedupe_nonempty(str(item.get("image_id") or "") for item in raw_items)
    valid_image_ids: set[str] = set()
    if image_ids:
        rows = await db.execute(
            select(Image.id).where(
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
                Image.id.in_(image_ids),
            )
        )
        valid_image_ids = set(rows.scalars().all())

    item_values = [
        values
        for raw in raw_items
        if str(raw.get("id") or "").strip() not in existing_item_ids
        if (
            values := _legacy_library_item_insert_values(
                user_id=user_id,
                raw=raw,
                valid_image_ids=valid_image_ids,
            )
        )
        is not None
    ]
    if item_values:
        await db.execute(
            pg_insert(ModelLibraryItem)
            .values(item_values)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        migrated = True

    if raw_hidden_ids:
        rows = await db.execute(
            select(ModelLibraryHiddenPreset.preset_id).where(
                ModelLibraryHiddenPreset.user_id == user_id,
                ModelLibraryHiddenPreset.preset_id.in_(raw_hidden_ids),
            )
        )
        existing_hidden = set(rows.scalars().all())
        hidden_values = [
            {"user_id": user_id, "preset_id": preset_id}
            for preset_id in raw_hidden_ids
            if preset_id not in existing_hidden
        ]
        if hidden_values:
            await db.execute(
                pg_insert(ModelLibraryHiddenPreset)
                .values(hidden_values)
                .on_conflict_do_nothing(index_elements=["user_id", "preset_id"])
            )
            migrated = True

    if migrated:
        await db.flush()
    return migrated


async def _load_user_library_items(
    db: AsyncSession, user_id: str
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(ModelLibraryItem, Image.metadata_jsonb)
            .join(Image, Image.id == ModelLibraryItem.image_id)
            .where(
                ModelLibraryItem.user_id == user_id,
                Image.deleted_at.is_(None),
            )
            .order_by(ModelLibraryItem.created_at.desc())
        )
    ).all()
    out: list[dict[str, Any]] = []
    for row, image_metadata_jsonb in rows:
        raw = _model_library_row_to_dict(row)
        raw["image_metadata_jsonb"] = (
            dict(image_metadata_jsonb) if isinstance(image_metadata_jsonb, dict) else {}
        )
        out.append(raw)
    return out


async def _load_user_hidden_preset_ids(
    db: AsyncSession, user_id: str
) -> set[str]:
    rows = (
        await db.execute(
            select(ModelLibraryHiddenPreset.preset_id).where(
                ModelLibraryHiddenPreset.user_id == user_id
            )
        )
    ).scalars().all()
    return {pid for pid in rows if isinstance(pid, str)}


async def _combined_library_items(
    db: AsyncSession, user_id: str
) -> tuple[list[dict[str, Any]], bool]:
    migrated = await _ensure_legacy_user_library_migrated(db, user_id)
    global_index = _load_global_library_index()
    hidden = await _load_user_hidden_preset_ids(db, user_id)
    preset_items = [
        dict(item)
        for item in global_index.get("preset_items", [])
        if isinstance(item, dict) and str(item.get("id") or "") not in hidden
    ]
    user_items = await _load_user_library_items(db, user_id)
    return [*preset_items, *user_items], migrated


def _filter_library_items(
    items: Iterable[dict[str, Any]],
    *,
    source: str,
    age_segment: str,
    appearance: str,
    q: str,
) -> list[dict[str, Any]]:
    query = q.strip().lower()
    filtered: list[dict[str, Any]] = []
    for item in items:
        item_source = str(item.get("source") or "")
        if source != "all" and item_source != source:
            continue
        item_age = _normalize_age_segment(item.get("age_segment"))
        if age_segment != "all" and item_age != age_segment:
            continue
        if appearance != "all":
            item_appearance = _normalize_appearance(item.get("appearance_direction"))
            if item_appearance != appearance:
                continue
        if query:
            haystack = " ".join(
                [
                    str(item.get("title") or ""),
                    str(item.get("gender") or ""),
                    str(item.get("appearance_direction") or ""),
                    " ".join(_clean_style_tags(item.get("style_tags") or item.get("tags") or [])),
                ]
            ).lower()
            if query not in haystack:
                continue
        filtered.append(item)
    source_rank = {"preset": 0, "favorite": 1, "user_upload": 2, "generated": 3}
    return sorted(
        filtered,
        key=lambda item: (
            source_rank.get(str(item.get("source") or ""), 9),
            _normalize_age_segment(item.get("age_segment")),
            str(item.get("title") or ""),
            str(item.get("id") or ""),
        ),
    )


async def _find_library_item(
    db: AsyncSession, *, user_id: str, item_id: str
) -> dict[str, Any] | None:
    """Resolve a library item by id. Presets come from the global JSON
    file; user items come from PostgreSQL. Hidden presets resolve to
    None for the asking user (they were "deleted" at user level).
    """
    await _ensure_legacy_user_library_migrated(db, user_id)
    if item_id.startswith("preset:") or not item_id.startswith("user:"):
        for item in _load_global_library_index().get("preset_items", []) or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "") != item_id:
                continue
            hidden = await _load_user_hidden_preset_ids(db, user_id)
            if item_id in hidden:
                return None
            return dict(item)
    if item_id.startswith("user:"):
        row = (
            await db.execute(
                select(ModelLibraryItem).where(
                    ModelLibraryItem.id == item_id,
                    ModelLibraryItem.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return _model_library_row_to_dict(row)
    return None


def _guess_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"


def _open_library_storage_file(storage_key: str) -> tuple[Path, str, str]:
    path = _storage_path(storage_key)
    if not path.is_file():
        raise _http("not_found", "library binary missing", 404)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, _guess_mime(path), sha


def _stream_file(path: Path) -> Iterable[bytes]:
    with path.open("rb") as f:
        while True:
            chunk = f.read(64 * 1024)
            if not chunk:
                break
            yield chunk


def _library_binary_response(storage_key: str, request: Request) -> Response:
    path, media_type, sha = _open_library_storage_file(storage_key)
    size = path.stat().st_size
    if size > MODEL_LIBRARY_MAX_BINARY_BYTES:
        # 拒绝异常大文件，防止恶意/损坏 preset 拖垮带宽（FastAPI 会按 413 返回）
        raise _http(
            "library_binary_too_large",
            f"library binary exceeds {MODEL_LIBRARY_MAX_BINARY_BYTES} bytes",
            413,
        )
    etag = f'"{sha}"'
    if request.headers.get("if-none-match") == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, max-age=86400"},
        )
    return StreamingResponse(
        _stream_file(path),
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=86400",
            "ETag": etag,
            "Content-Length": str(size),
        },
    )


def _preset_storage_key(preset_id: str, version: int, image_path: str) -> str:
    suffix = Path(image_path).suffix.lower() or ".webp"
    return f"{MODEL_LIBRARY_ROOT_KEY}/presets/{preset_id}/v{version}{suffix}"


def _preset_thumb_storage_key(preset_id: str, thumb_path: str | None, image_key: str) -> str:
    if not thumb_path:
        return image_key
    suffix = Path(thumb_path).suffix.lower() or ".webp"
    return f"{MODEL_LIBRARY_ROOT_KEY}/presets/{preset_id}/thumb{suffix}"


def _write_bytes_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


async def _fetch_bytes(client: httpx.AsyncClient, url: str) -> bytes:
    resp = await client.get(url)
    resp.raise_for_status()
    return resp.content


def _github_api_child_url(base_url: str, child_name: str) -> str:
    prefix, _, query = base_url.partition("?")
    return (
        f"{prefix.rstrip('/')}/{child_name}?{query}"
        if query
        else f"{prefix.rstrip('/')}/{child_name}"
    )


async def _walk_github_contents(
    client: httpx.AsyncClient,
    contents_url: str,
) -> list[dict[str, Any]]:
    resp = await client.get(
        contents_url,
        headers={"Accept": "application/vnd.github+json"},
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("type") == "file":
        return [data]
    if not isinstance(data, list):
        raise ValueError("GitHub contents response must be an array")
    files: list[dict[str, Any]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        name = str(entry.get("name") or "")
        if entry_type == "dir":
            child_url = str(entry.get("url") or "") or _github_api_child_url(contents_url, name)
            files.extend(await _walk_github_contents(client, child_url))
        elif entry_type == "file":
            files.append(entry)
    return files


def _metadata_from_github_file(entry: dict[str, Any]) -> dict[str, Any] | None:
    path_value = str(entry.get("path") or entry.get("name") or "").strip()
    if not path_value:
        return None
    path = Path(path_value)
    suffix = path.suffix.lower()
    if suffix not in MODEL_LIBRARY_IMAGE_SUFFIXES:
        return None
    stem = path.stem
    if stem.endswith(".thumb"):
        return None
    download_url = str(entry.get("download_url") or "").strip()
    if not download_url:
        return None
    path_parts = [
        part for part in path.parts if part not in {"assets", "apparel-model-presets"}
    ]
    parent_dirs = path_parts[:-1]
    age_segment = next(
        (
            age
            for part in reversed(parent_dirs)
            if (age := _age_segment_from_folder_name(part)) is not None
        ),
        "user_favorites",
    )
    gender_from_folder = next(
        (
            gender_value
            for part in reversed(parent_dirs)
            if (gender_value := _gender_from_folder_name(part)) is not None
        ),
        None,
    )
    preset_id = _preset_id_from_path(path_value)
    lower_name = path.name.lower()
    gender = gender_from_folder
    if any(token in lower_name for token in ("female", "woman", "girl")):
        gender = "female"
    elif any(token in lower_name for token in ("male", "man", "boy")):
        gender = "male"
    normalized_name = re.sub(r"[_\s]+", "-", lower_name)
    appearance = None
    for token, value in (
        ("southeast-asian", "southeast_asian"),
        ("south-asian", "south_asian"),
        ("east-asian", "east_asian"),
        ("middle-eastern", "middle_eastern"),
        ("middle-east", "middle_eastern"),
        ("european", "european"),
        ("latin", "latin"),
        ("african", "african"),
        ("asian", "asian"),
    ):
        if token in normalized_name:
            appearance = value
            break
    words = [
        part
        for part in re.split(r"[-_]+", path.stem)
        if part and not part.isdigit() and part not in {age_segment, "female", "male", "woman", "man"}
    ]
    return {
        "preset_id": preset_id,
        "version": 1,
        "title": _title_from_preset_id(preset_id),
        "age_segment": age_segment,
        "library_folder": _model_library_folder_for_age(age_segment, gender),
        "gender": gender,
        "appearance_direction": appearance,
        "style_tags": _clean_style_tags(words[:6]),
        "image_path": path_value,
        "download_url": download_url,
        "sha": _clean_optional_text(entry.get("sha"), max_len=80),
        "prompt_hint": _title_from_preset_id(preset_id),
    }


def _cached_sync_response(state: dict[str, Any]) -> ApparelModelLibrarySyncOut:
    """从 sync state 拼装一个 'skipped' 响应，用于 cooldown 命中时返回。"""
    result = state.get("last_result") if isinstance(state.get("last_result"), dict) else {}
    return ApparelModelLibrarySyncOut(
        status="skipped",
        added=int(result.get("added") or 0),
        updated=int(result.get("updated") or 0),
        skipped=int(result.get("skipped") or 0),
        errors=_clean_string_list(
            result.get("errors") or [],
            max_items=20,
            max_len=300,
        ),
        last_success_at=_safe_datetime(state.get("last_success_at")),
        last_error=_clean_optional_text(state.get("last_error"), max_len=1000),
    )


async def _sync_library_presets_from_github_folder(
    contents_url: str,
    *,
    proxy_url: str | None = None,
) -> ApparelModelLibrarySyncOut:
    if not contents_url:
        raise _http("sync_not_configured", "preset GitHub folder url is not configured", 503)
    # _SYNC_LOCK 防同进程并发；cooldown 用 last_success_at（5min）和
    # last_attempt_at（30s 失败重试保护），避免失败被锁死或滥用 hammer GitHub。
    async with _SYNC_LOCK:
        state = _read_json_file(_library_sync_state_path(), _default_sync_state())
        last_success = _safe_datetime(state.get("last_success_at"))
        if last_success is not None:
            success_age = (_now() - last_success).total_seconds()
            if success_age < MODEL_LIBRARY_SYNC_COOLDOWN_SECONDS:
                return _cached_sync_response(state)
        last_attempt = _safe_datetime(state.get("last_attempt_at"))
        if last_attempt is not None:
            attempt_age = (_now() - last_attempt).total_seconds()
            if attempt_age < MODEL_LIBRARY_SYNC_RETRY_COOLDOWN_SECONDS:
                return _cached_sync_response(state)
        return await _do_sync_library_presets(contents_url, state, proxy_url=proxy_url)


async def _do_sync_library_presets(
    contents_url: str,
    state: dict[str, Any],
    *,
    proxy_url: str | None = None,
) -> ApparelModelLibrarySyncOut:
    now = _now()
    state["last_attempt_at"] = now.isoformat().replace("+00:00", "Z")
    _save_sync_state(state)

    added = 0
    updated = 0
    skipped = 0
    errors: list[str] = []
    try:
        async with httpx.AsyncClient(
            **_model_library_http_client_kwargs(proxy_url)
        ) as client:
            files = await _walk_github_contents(client, contents_url)
            parsed_items = [
                item
                for item in (_metadata_from_github_file(entry) for entry in files)
                if item is not None
            ]
            thumb_by_base: dict[str, dict[str, Any]] = {}
            for entry in files:
                path_value = str(entry.get("path") or entry.get("name") or "")
                path = Path(path_value)
                if path.suffix.lower() not in MODEL_LIBRARY_IMAGE_SUFFIXES:
                    continue
                if not path.stem.endswith(".thumb"):
                    continue
                base = str(path.with_name(f"{path.stem[:-len('.thumb')]}{path.suffix}"))
                thumb_by_base[base] = entry

            index = _load_global_library_index()
            existing_by_id = {
                str(item.get("id") or ""): dict(item)
                for item in index.get("preset_items", [])
                if isinstance(item, dict)
            }
            next_items = existing_by_id
            for parsed in parsed_items:
                preset_id = parsed["preset_id"]
                version = int(parsed["version"])
                item_id = f"preset:{preset_id}:v{version}"
                image_key = _preset_storage_key(
                    preset_id, version, str(parsed["image_path"])
                )
                thumb_entry = thumb_by_base.get(str(parsed["image_path"]))
                thumb_key = _preset_thumb_storage_key(
                    preset_id,
                    str(thumb_entry.get("path")) if thumb_entry else None,
                    image_key,
                )

                image_url = str(parsed["download_url"])
                try:
                    data = await _fetch_bytes(client, image_url)
                except Exception as exc:  # noqa: BLE001
                    skipped += 1
                    errors.append(f"{preset_id}: image download failed: {exc!r}")
                    continue
                actual_sha = hashlib.sha256(data).hexdigest()
                image_path = _storage_path(image_key)
                previous = next_items.get(item_id)
                needs_image_write = not image_path.is_file()
                if previous and previous.get("sha256") != actual_sha:
                    needs_image_write = True
                if needs_image_write:
                    _write_bytes_replace(image_path, data)

                if thumb_entry:
                    thumb_path = _storage_path(thumb_key)
                    thumb_url = str(thumb_entry.get("download_url") or "")
                    try:
                        thumb_data = await _fetch_bytes(client, thumb_url)
                        thumb_sha = hashlib.sha256(thumb_data).hexdigest()
                        if not thumb_path.is_file() or (
                            previous and previous.get("thumb_sha256") != thumb_sha
                        ):
                            _write_bytes_replace(thumb_path, thumb_data)
                    except Exception as exc:  # noqa: BLE001
                        thumb_key = image_key
                        thumb_sha = actual_sha
                        errors.append(f"{preset_id}: thumb fallback to original: {exc!r}")
                else:
                    thumb_sha = actual_sha

                item = {
                    "id": item_id,
                    "source": "preset",
                    "preset_id": preset_id,
                    "version": version,
                    "title": parsed["title"],
                    "age_segment": parsed["age_segment"],
                    "library_folder": parsed["library_folder"],
                    "gender": parsed["gender"],
                    "appearance_direction": parsed["appearance_direction"],
                    "style_tags": parsed["style_tags"],
                    "image_storage_key": image_key,
                    "thumb_storage_key": thumb_key,
                    "sha256": actual_sha,
                    "thumb_sha256": thumb_sha,
                    "prompt_hint": parsed["prompt_hint"],
                    "github_image_path": parsed["image_path"],
                    "github_thumb_path": str(thumb_entry.get("path")) if thumb_entry else None,
                    "github_sha": parsed.get("sha"),
                    "created_at": (previous or {}).get("created_at") or _iso_now(),
                    "updated_at": _iso_now(),
                }
                if previous is None:
                    added += 1
                elif {
                    k: previous.get(k)
                    for k in (
                        "title",
                        "age_segment",
                        "gender",
                        "appearance_direction",
                        "style_tags",
                        "sha256",
                        "thumb_sha256",
                        "prompt_hint",
                    )
                } != {
                    k: item.get(k)
                    for k in (
                        "title",
                        "age_segment",
                        "gender",
                        "appearance_direction",
                        "style_tags",
                        "sha256",
                        "thumb_sha256",
                        "prompt_hint",
                    )
                }:
                    updated += 1
                else:
                    skipped += 1
                next_items[item_id] = item

        index["preset_items"] = sorted(
            next_items.values(),
            key=lambda item: (
                _normalize_age_segment(item.get("age_segment")),
                str(item.get("preset_id") or ""),
                int(item.get("version") or 0),
            ),
        )
        _save_global_library_index(index)
        state = _read_json_file(_library_sync_state_path(), _default_sync_state())
        state["last_success_at"] = now.isoformat().replace("+00:00", "Z")
        state["last_error"] = None
        state["last_result"] = {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "errors": errors[:20],
        }
        _save_sync_state(state)
        return ApparelModelLibrarySyncOut(
            status="ok",
            added=added,
            updated=updated,
            skipped=skipped,
            errors=errors[:20],
            last_success_at=now,
            last_error=None,
        )
    except Exception as exc:
        state = _read_json_file(_library_sync_state_path(), _default_sync_state())
        msg = str(exc)
        state["last_error"] = msg[:1000]
        state["last_result"] = {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "errors": [*errors[:19], msg[:300]],
        }
        _save_sync_state(state)
        if isinstance(exc, HTTPException):
            raise
        raise _http("preset_sync_failed", msg or "preset sync failed", 502) from exc


def _showcase_reference_image_ids(
    *,
    product_image_ids: Iterable[str],
    model_image_id: str | None,
    selected_accessory_image_id: str | None,
) -> list[str]:
    model_reference_id = selected_accessory_image_id or model_image_id
    return _dedupe_nonempty(
        [
            *product_image_ids,
            model_reference_id or "",
        ]
    )


def _showcase_target_image_count(
    *,
    existing_image_ids: Iterable[str],
    output_count: int,
) -> int:
    return len(_dedupe_nonempty(existing_image_ids)) + output_count


def _showcase_expected_image_count(
    *,
    showcase_input: dict[str, Any],
    fallback_task_count: int,
) -> int:
    return int(
        showcase_input.get("target_image_count")
        or showcase_input.get("output_count")
        or fallback_task_count
    )


async def _validate_owned_images(
    db: AsyncSession,
    *,
    user_id: str,
    image_ids: list[str],
    min_count: int = 1,
    max_count: int | None = None,
) -> list[str]:
    ids = _dedupe_nonempty(image_ids)
    if len(ids) < min_count:
        raise _http("missing_image", f"at least {min_count} image required", 422)
    if max_count is not None and len(ids) > max_count:
        raise _http("too_many_images", f"at most {max_count} images allowed", 422)
    rows = (
        await db.execute(
            select(Image.id).where(
                Image.id.in_(ids),
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    if set(rows) != set(ids):
        raise _http(
            "invalid_image",
            "one or more images are not owned by the current user or were deleted",
            400,
        )
    return ids


async def _owned_image(db: AsyncSession, *, user_id: str, image_id: str) -> Image:
    img = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if img is None:
        raise _http("invalid_image", "image is not owned by current user or was deleted", 400)
    return img


def _image_url(image_id: str) -> str:
    return f"/api/images/{image_id}/binary"


def _image_variant_url(image_id: str, kind: str = "display2048") -> str:
    return f"/api/images/{image_id}/variants/{kind}"


def _model_library_download_filename(
    *,
    image_id: str,
    mime: str | None,
    age_segment: str | None,
    gender: str | None,
    appearance_direction: str | None,
    style_tags: list[str],
) -> str:
    ext = "png"
    if isinstance(mime, str) and mime.startswith("image/"):
        ext = "jpg" if mime == "image/jpeg" else mime.removeprefix("image/")
    return model_image_filename(
        image_id=image_id,
        ext=ext,
        age_segment=age_segment,
        gender=gender,
        appearance_direction=appearance_direction,
        style_tags=style_tags,
    )


def _model_library_image_metadata_from_fields(
    *,
    image_id: str,
    age_segment: str | None,
    gender: str | None,
    appearance_direction: str | None,
    style_tags: list[str],
    prompt_hint: str | None = None,
    source: str = "model_library",
    mime: str | None = None,
) -> dict[str, Any]:
    payload = build_model_image_metadata(
        age_segment=age_segment,
        gender=gender,
        appearance_direction=appearance_direction,
        style_tags=style_tags,
        source=source,
        prompt_hint=prompt_hint,
    )
    return {
        "model_library": payload,
        "suggested_filename": _model_library_download_filename(
            image_id=image_id,
            mime=mime,
            age_segment=age_segment,
            gender=gender,
            appearance_direction=appearance_direction,
            style_tags=style_tags,
        ),
    }


async def _create_user_image_from_preset(
    db: AsyncSession,
    *,
    user_id: str,
    item: dict[str, Any],
) -> Image:
    item_id = str(item.get("id") or "").strip()
    existing = (
        await db.execute(
            select(Image).where(
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
                Image.metadata_jsonb["apparel_model_library_item_id"].astext == item_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    image_key = str(item.get("image_storage_key") or "").strip()
    path = _storage_path(image_key)
    if not path.is_file():
        raise _http("not_found", "preset image binary is missing", 404)
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    width = 0
    height = 0
    try:
        with PILImage.open(path) as im:
            width, height = im.size
    except Exception:
        logger.warning("failed to inspect preset image dimensions key=%s", image_key)
    image_id = new_uuid7()
    suffix = path.suffix.lower() or ".webp"
    copy_key = f"u/{user_id}/apparel-model-library/{image_id}{suffix}"
    # 先把字节落盘，再写 DB 行：避免 DB 行存在但二进制 404 的孤儿
    copy_path = _storage_path(copy_key)
    _write_bytes_replace(copy_path, data)
    try:
        img = Image(
            id=image_id,
            user_id=user_id,
            source=ImageSource.UPLOADED.value,
            storage_key=copy_key,
            mime=_guess_mime(path),
            width=width,
            height=height,
            size_bytes=copy_path.stat().st_size,
            sha256=sha,
            blurhash=None,
            visibility=ImageVisibility.PRIVATE.value,
            metadata_jsonb={
                "apparel_model_library_item_id": item_id,
                "apparel_model_library_source": "preset",
                "preset_id": item.get("preset_id"),
                "preset_version": item.get("version"),
                "cached_from_storage_key": image_key,
                "shared_storage": False,
            },
        )
        db.add(img)
        await db.flush()
    except Exception:
        # DB flush 失败时清理刚写的孤儿文件，避免下次重试时 sha 命中残留路径
        copy_path.unlink(missing_ok=True)
        raise
    return img


def _library_item_to_user_index_entry(
    *,
    item_id: str,
    source: str,
    image_id: str,
    title: str,
    age_segment: str,
    gender: str | None,
    appearance_direction: str | None,
    style_tags: list[str],
) -> dict[str, Any]:
    now = _iso_now()
    normalized_age = _normalize_age_segment(age_segment)
    normalized_gender = _normalize_model_gender(gender)
    return {
        "id": item_id,
        "source": source,
        "title": title.strip()[:120],
        "age_segment": normalized_age,
        "library_folder": _model_library_folder_for_age(normalized_age, normalized_gender),
        "gender": normalized_gender,
        "appearance_direction": _clean_optional_text(appearance_direction, max_len=80),
        "style_tags": _clean_style_tags(style_tags),
        "image_id": image_id,
        "owner_user_id": "",
        "created_at": now,
        "updated_at": now,
    }


def _user_index_entry_from_image(
    *,
    user_id: str,
    source: str,
    image_id: str,
    title: str,
    age_segment: str,
    gender: str | None,
    appearance_direction: str | None,
    style_tags: list[str],
) -> dict[str, Any]:
    item = _library_item_to_user_index_entry(
        item_id=f"user:{new_uuid7()}",
        source=source,
        image_id=image_id,
        title=title,
        age_segment=age_segment,
        gender=gender,
        appearance_direction=appearance_direction,
        style_tags=style_tags,
    )
    item["owner_user_id"] = user_id
    return item


async def _add_user_library_item(
    db: AsyncSession,
    *,
    user_id: str,
    source: str,
    image_id: str,
    title: str,
    age_segment: str,
    gender: str | None,
    appearance_direction: str | None,
    style_tags: list[str],
) -> dict[str, Any]:
    """Insert one row into ``model_library_items``. Each call is a
    standalone INSERT — concurrent favorites no longer race a shared
    JSON file.
    """
    image = await _owned_image(db, user_id=user_id, image_id=image_id)
    normalized_age = _normalize_age_segment(age_segment)
    normalized_gender = _normalize_model_gender(gender)
    cleaned_appearance = _clean_optional_text(appearance_direction, max_len=80)
    cleaned_tags = _clean_style_tags(style_tags)
    metadata_jsonb = _model_library_image_metadata_from_fields(
        image_id=image_id,
        age_segment=normalized_age,
        gender=normalized_gender,
        appearance_direction=cleaned_appearance,
        style_tags=cleaned_tags,
        prompt_hint=title,
        source=source,
        mime=getattr(image, "mime", None),
    )
    row = ModelLibraryItem(
        id=f"user:{new_uuid7()}",
        user_id=user_id,
        source=source,
        image_id=image_id,
        title=title.strip()[:120],
        age_segment=normalized_age,
        gender=normalized_gender,
        appearance_direction=cleaned_appearance,
        style_tags=cleaned_tags,
        library_folder=_model_library_folder_for_age(
            normalized_age, normalized_gender
        ),
        metadata_jsonb=metadata_jsonb,
    )
    image_metadata = dict(getattr(image, "metadata_jsonb", None) or {})
    image_metadata.update(metadata_jsonb)
    image.metadata_jsonb = image_metadata
    db.add(row)
    await db.flush()
    return _model_library_row_to_dict(row)


def _primary_candidate_image_id(candidate: ModelCandidate) -> str | None:
    if candidate.contact_sheet_image_id:
        return candidate.contact_sheet_image_id
    brief = candidate.model_brief_json or {}
    candidate_image_ids = brief.get("candidate_image_ids")
    if isinstance(candidate_image_ids, list):
        for image_id in candidate_image_ids:
            if isinstance(image_id, str) and image_id:
                return image_id
    return None


def _infer_age_segment_from_workflow(run: WorkflowRun) -> str:
    meta = run.metadata_jsonb or {}
    profile = meta.get("model_profile")
    if isinstance(profile, dict):
        age = _normalize_age_segment(profile.get("age_segment"))
        if age != "user_favorites":
            return age
    return _infer_age_segment_from_text(run.user_prompt or "")


def _metadata_model_profile_from_prompt(text: str) -> dict[str, Any]:
    gender = None
    if "女性" in text or "女" in text:
        gender = "female"
    elif "男性" in text or "男" in text:
        gender = "male"
    appearance = None
    for zh, value in (
        ("欧美", "european"),
        ("亚洲", "asian"),
        ("拉美", "latin"),
        ("中东", "middle_eastern"),
        ("非洲", "african"),
    ):
        if zh in text:
            appearance = value
            break
    return {
        "age_segment": _normalize_age_segment(_infer_age_segment_from_text(text)),
        "gender": gender,
        "appearance_direction": appearance,
    }


def _infer_age_segment_from_text(text: str) -> str:
    if "幼儿" in text:
        return "toddler"
    if any(word in text for word in ("儿童", "童装", "小朋友", "孩子")):
        return "child"
    if "青少年" in text:
        return "teen"
    if "青年" in text:
        return "young_adult"
    if "中年" in text or "中老年" in text:
        return "middle_aged"
    if "老年" in text:
        return "senior"
    if "熟龄" in text or "成年" in text:
        return "adult"
    return "user_favorites"


async def _get_owned_conversation(
    db: AsyncSession,
    *,
    user_id: str,
    conversation_id: str,
) -> Conversation:
    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise _http("not_found", "conversation not found", 404)
    return conv


async def _get_or_create_workflow_conversation(
    db: AsyncSession,
    *,
    user: User,
    conversation_id: str | None,
    title: str,
    workflow_type: str = WORKFLOW_TYPE,
) -> Conversation:
    if conversation_id:
        conv = await _get_owned_conversation(
            db, user_id=user.id, conversation_id=conversation_id
        )
        params = dict(conv.default_params or {})
        params["workflow_type"] = workflow_type
        params["hidden_from_conversations"] = True
        conv.default_params = params
        return conv
    conv = Conversation(
        user_id=user.id,
        title=title,
        archived=True,
        default_params={
            "workflow_type": workflow_type,
            "hidden_from_conversations": True,
        },
    )
    db.add(conv)
    await db.flush()
    return conv


async def _get_run(
    db: AsyncSession,
    *,
    user_id: str,
    run_id: str,
    lock: bool = False,
) -> WorkflowRun:
    stmt = select(WorkflowRun).where(
        WorkflowRun.id == run_id,
        WorkflowRun.user_id == user_id,
        WorkflowRun.deleted_at.is_(None),
    )
    if lock:
        stmt = stmt.with_for_update()
    run = (await db.execute(stmt)).scalar_one_or_none()
    if run is None:
        raise _http("not_found", "workflow not found", 404)
    return run


async def _load_steps(db: AsyncSession, run_id: str) -> list[WorkflowStep]:
    rows = (
        await db.execute(
            select(WorkflowStep).where(WorkflowStep.workflow_run_id == run_id)
        )
    ).scalars().all()
    # apparel 与 poster 的 step_key 互不重叠；合并成一张顺序表，
    # 未识别的 key 保留尾部稳定顺序。
    order: dict[str, int] = {}
    for idx, key in enumerate(WORKFLOW_STEPS):
        order[key] = idx
    for idx, key in enumerate(POSTER_WORKFLOW_STEPS):
        order[key] = len(WORKFLOW_STEPS) + idx
    return sorted(rows, key=lambda s: order.get(s.step_key, 999))


async def _step(db: AsyncSession, run_id: str, step_key: str) -> WorkflowStep:
    row = (
        await db.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run_id,
                WorkflowStep.step_key == step_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("workflow_corrupt", f"missing workflow step: {step_key}", 500)
    return row


def _task_error_summary(rows: Iterable[Any], fallback: str) -> str:
    messages: list[str] = []
    for row in rows:
        raw_message = getattr(row, "error_message", None)
        raw_code = getattr(row, "error_code", None)
        message = str(raw_message).strip() if raw_message else ""
        code = str(raw_code).strip() if raw_code else ""
        if message and code and code not in message:
            messages.append(f"{code}: {message}")
        elif message:
            messages.append(message)
        elif code:
            messages.append(code)
    return "；".join(_dedupe_nonempty(messages))[:2000] or fallback


async def _workflow_steps_and_candidates(
    db: AsyncSession,
    run: WorkflowRun,
) -> tuple[list[WorkflowStep], list[ModelCandidate]]:
    steps = await _load_steps(db, run.id)
    candidates = list(
        (
            await db.execute(
                select(ModelCandidate).where(ModelCandidate.workflow_run_id == run.id)
            )
        ).scalars().all()
    )
    return steps, candidates


def _workflow_direct_task_ids(
    steps: Iterable[WorkflowStep],
    candidates: Iterable[ModelCandidate],
) -> list[str]:
    return _dedupe_nonempty(
        [
            *(task_id for step in steps for task_id in (step.task_ids or [])),
            *(task_id for candidate in candidates for task_id in (candidate.task_ids or [])),
        ]
    )


def _workflow_direct_image_ids(
    steps: Iterable[WorkflowStep],
    candidates: Iterable[ModelCandidate],
) -> list[str]:
    return _dedupe_nonempty(
        [
            *(image_id for step in steps for image_id in (step.image_ids or [])),
            *(
                image_id
                for candidate in candidates
                for image_id in _candidate_reference_image_ids(candidate)
            ),
        ]
    )


def _candidate_reference_image_ids(candidate: ModelCandidate) -> list[str]:
    brief = getattr(candidate, "model_brief_json", None) or {}
    raw_candidate_ids = brief.get("candidate_image_ids")
    candidate_image_ids = raw_candidate_ids if isinstance(raw_candidate_ids, list) else []
    return _dedupe_nonempty(
        [
            *(image_id for image_id in candidate_image_ids if isinstance(image_id, str)),
            candidate.contact_sheet_image_id,
            candidate.portrait_image_id,
            candidate.front_image_id,
            candidate.side_image_id,
            candidate.back_image_id,
        ]
    )


async def _workflow_generation_ids_from_task_ids(
    db: AsyncSession,
    *,
    user_id: str,
    task_ids: list[str],
) -> list[str]:
    generations = await _workflow_generation_rows_from_task_ids(
        db,
        user_id=user_id,
        task_ids=task_ids,
        include_dual_bonus=True,
    )
    return _dedupe_nonempty(generation.id for generation in generations)


async def _workflow_generation_rows_from_task_ids(
    db: AsyncSession,
    *,
    user_id: str,
    task_ids: list[str],
    include_dual_bonus: bool,
) -> list[Generation]:
    task_ids = _dedupe_nonempty(task_ids)
    if not task_ids:
        return []
    base_generations = list(
        (
            await db.execute(
                select(Generation).where(
                    Generation.user_id == user_id,
                    Generation.id.in_(task_ids),
                )
            )
        ).scalars().all()
    )
    if not include_dual_bonus:
        return base_generations
    bonus_generations = list(
        (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == user_id,
                    Generation.upstream_request["parent_generation_id"].astext.in_(
                        task_ids
                    ),
                    Generation.upstream_request["is_dual_race_bonus"]
                    .as_boolean()
                    .is_(True),
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        ).scalars().all()
    )
    return [*base_generations, *bonus_generations]


async def _soft_delete_workflow_generated_images(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    deleted_at: datetime,
    cancel_message: str,
) -> dict[str, int]:
    """Soft-delete images produced by a workflow and cancel its active tasks.

    Images explicitly saved into the user's model library are preserved; those
    are no longer just transient task outputs.
    """
    steps, candidates = await _workflow_steps_and_candidates(db, run)
    task_ids = _workflow_direct_task_ids(steps, candidates)
    image_ids = _workflow_direct_image_ids(steps, candidates)
    generation_ids = await _workflow_generation_ids_from_task_ids(
        db, user_id=run.user_id, task_ids=task_ids
    )

    canceled_generations = 0
    if generation_ids:
        result = await db.execute(
            update(Generation)
            .where(
                Generation.user_id == run.user_id,
                Generation.id.in_(generation_ids),
                Generation.status.in_(
                    [GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value]
                ),
            )
            .values(
                status=GenerationStatus.CANCELED.value,
                progress_stage="finalizing",
                finished_at=deleted_at,
                error_code="cancelled",
                error_message=cancel_message,
            )
            .execution_options(synchronize_session=False)
        )
        canceled_generations = int(result.rowcount or 0)

    canceled_completions = 0
    if task_ids:
        result = await db.execute(
            update(Completion)
            .where(
                Completion.user_id == run.user_id,
                Completion.id.in_(task_ids),
                Completion.status.in_(
                    [CompletionStatus.QUEUED.value, CompletionStatus.RUNNING.value]
                ),
            )
            .values(
                status=CompletionStatus.CANCELED.value,
                progress_stage="finalizing",
                finished_at=deleted_at,
                error_code="cancelled",
                error_message=cancel_message,
            )
            .execution_options(synchronize_session=False)
        )
        canceled_completions = int(result.rowcount or 0)

    deleted_images = 0
    image_matchers = []
    if generation_ids:
        image_matchers.append(Image.owner_generation_id.in_(generation_ids))
    if image_ids:
        image_matchers.append(Image.id.in_(image_ids))
    if image_matchers:
        preserved_library_images = select(ModelLibraryItem.image_id).where(
            ModelLibraryItem.user_id == run.user_id,
            ModelLibraryItem.image_id.is_not(None),
        )
        result = await db.execute(
            update(Image)
            .where(
                Image.user_id == run.user_id,
                Image.deleted_at.is_(None),
                or_(*image_matchers),
                ~Image.id.in_(preserved_library_images),
            )
            .values(deleted_at=deleted_at)
            .execution_options(synchronize_session=False)
        )
        deleted_images = int(result.rowcount or 0)

    return {
        "images_deleted": deleted_images,
        "generations_canceled": canceled_generations,
        "completions_canceled": canceled_completions,
    }


def _seed_steps(run: WorkflowRun, *, user_prompt: str) -> list[WorkflowStep]:
    steps: list[WorkflowStep] = []
    for key in WORKFLOW_STEPS:
        status = "waiting_input"
        input_json: dict[str, Any] = {}
        output_json: dict[str, Any] = {}
        if key == "upload_product":
            status = "approved"
            input_json = {
                "product_image_ids": run.product_image_ids,
                "user_prompt": user_prompt,
            }
            output_json = {"confirmed": True}
        elif key == "product_analysis":
            status = "running"
            input_json = {
                "product_image_ids": run.product_image_ids,
                "user_prompt": user_prompt,
                "prompt_contract": "extract visible apparel constraints as structured JSON",
            }
        steps.append(
            WorkflowStep(
                workflow_run_id=run.id,
                step_key=key,
                status=status,
                input_json=input_json,
                output_json=output_json,
            )
        )
    return steps


def _product_analysis_prompt(user_prompt: str) -> str:
    return (
        "请分析上传的服饰白底商品图。这个步骤只服务后续生成真人模特穿搭图，"
        "不要写复杂营销文案，只提取最终提示词真正需要的信息。只描述图片中可见信息，"
        "不确定填 unknown。"
        "必须只返回一个 JSON object，不要 Markdown，不要代码块，不要解释文字。"
        "字段固定为：category, color, material_guess, silhouette, "
        "key_details, must_preserve, styling_recommendations, background_recommendation, risks。"
        "除 unknown 和字段名外，内容用简体中文。must_preserve 只列 3-8 个后续生成必须完全还原的"
        "视觉点，例如颜色、版型、领口、袖型、衣长、面料观感、图案/logo、纽扣、口袋、拉链、缝线/拼接。"
        "styling_recommendations 只给 1-3 个低存在感、适合商品和用户方向的配饰/搭配建议，"
        "用来让整体更搭配，不要遮挡衣服主体。background_recommendation 给 1 句与衣服风格匹配的"
        "开放式背景氛围建议，不要列具体地点或具体空间名，适合亚马逊/电商主图。risks 只列会影响商品还原的风险。"
        f"用户方向：{user_prompt or '高级电商服饰模特展示图'}"
    )


def _candidate_prompt(
    *,
    style_prompt: str,
    product_analysis: dict[str, Any],
    candidate_index: int,
    avoid: list[str],
) -> str:
    product_category = str(product_analysis.get("category") or "adult apparel")
    base_styling = "warm ivory sleeveless top and warm ivory shorts, barefoot"
    style = style_prompt.strip() or "clean premium ecommerce model, refined, natural"
    age_requirement = _age_direction(style)
    height_requirement = _height_requirement(style)
    avoid_text = ", ".join(item.strip() for item in avoid if item and item.strip())
    avoid_line = f"Avoid: {avoid_text}." if avoid_text else ""
    diversity = _model_diversity_anchor(
        candidate_index=candidate_index,
        gender=_infer_candidate_gender(style_prompt, product_analysis),
    )
    return " ".join(
        part
        for part in [
            "Create one clean 2x2 ecommerce model reference contact sheet, exactly four panels: "
            "top-left front full body, top-right left 90-degree profile full body, "
            "bottom-left straight back full body, bottom-right close-up headshot.",
            "Same model in all four panels, consistent framing, "
            "same camera height and distance for the three full-body views.",
            "Side panel must be a true left profile (only one eye visible, "
            "body fully sideways, not a three-quarter pose).",
            "Back panel must hide the face. Headshot must be straight frontal with both eyes visible.",
            "Plain seamless white or light gray studio background, soft even lighting, "
            "no props, no text labels.",
            "Real commercially photographed person, not an AI beauty render.",
            "The model is not wearing the user's product yet.",
            f"Use simple neutral base clothing: {base_styling}.",
            "Every candidate must wear this exact same outfit; "
            "only face, hair, and body type may differ between candidates.",
            f"{age_requirement} {height_requirement}".strip(),
            diversity,
            "No text labels, no height labels, no watermark, no logo, no celebrity likeness.",
            f"Style direction: {style}.",
            f"Product category context: {product_category}.",
            f"Candidate variation number: {candidate_index}.",
            avoid_line,
        ]
        if part
    ).strip()


def _showcase_prompt(
    *,
    product_analysis: dict[str, Any],
    selected_candidate: ModelCandidate,
    accessory_plan: dict[str, Any],
    template: str,
    shot_type: str,
    final_quality: str,
    user_prompt: str = "",
    shot_variant: ShotVariant | None = None,
    age_segment: str | None = None,
    aspect_ratio: str = "4:5",
    scene_environment: str = "indoor",
) -> str:
    brief = selected_candidate.model_brief_json or {}
    summary = str(brief.get("summary") or user_prompt or "自然电商模特")
    must_preserve = product_analysis.get("must_preserve")
    fallback_preserve = "颜色、版型、款式、领口、袖型、衣长、图案/logo、纽扣/拉链/口袋/缝线"
    preserve_items = (
        [str(item).strip() for item in must_preserve if str(item).strip()]
        if isinstance(must_preserve, list)
        else []
    )
    product_preserve = "、".join(preserve_items[:8]) or fallback_preserve
    height_cm_raw = brief.get("height_cm")
    try:
        height_cm = (
            int(height_cm_raw)
            if height_cm_raw is not None
            else _infer_model_height_cm(" ".join(part for part in (summary, user_prompt) if part))
        )
    except (TypeError, ValueError):
        height_cm = _infer_model_height_cm(
            " ".join(part for part in (summary, user_prompt) if part)
        )
    model_consistency = (
        "保持同一张脸、发型、肤色、年龄感和身材比例一致，不要换人。"
        f"身高 {height_cm}cm，头身比和肢体长度沿用参考模特。"
        f"模特方向：{summary}。"
    )
    accessory_direction = "少量自然搭配，不要抢衣服主体；如果附件中包含已选配饰四宫格，优先参考它。"
    if shot_variant is None:
        shot_variant = _showcase_default_variant(template, shot_type, age_segment)
    shot_direction = shot_variant["label"] if shot_variant else shot_type
    framing = shot_variant["framing"] if shot_variant else "product_first"
    framing_direction = _showcase_framing_direction(shot_type, framing, aspect_ratio)
    composition_extra = _showcase_composition_direction(template)
    if composition_extra:
        framing_direction = f"{framing_direction}；{composition_extra}"
    pose_direction = _showcase_pose_direction(template)
    soft = _age_soft_constraint(age_segment)
    if soft:
        pose_direction = f"{pose_direction}，{soft}"
    quality_direction = "4K 终稿" if final_quality == "4k" else "高质量"
    style_region = _style_region_from_text(summary)
    return _showcase_prompt_brief(
        user_direction=user_prompt,
        template_direction=_template_requirement(template, product_analysis, scene_environment),
        product_preserve=product_preserve,
        accessory_direction=accessory_direction,
        model_consistency=model_consistency,
        shot_direction=shot_direction,
        pose_direction=pose_direction,
        framing_direction=framing_direction,
        quality_direction=quality_direction,
        render_direction=_showcase_render_direction(template, scene_environment),
        style_region=style_region,
    )


def _showcase_default_variant(
    template: str,
    shot_type: str,
    age_segment: str | None,
) -> ShotVariant | None:
    band = _resolve_pool_band(age_segment)
    pool = SHOT_POOL_BY_BAND.get(band, ADULT_POOL)
    template_pool = pool.get(template) or ADULT_POOL.get(template)  # type: ignore[arg-type]
    if not template_pool:
        return None
    variants = template_pool.get(shot_type) or template_pool.get(SHOT_CLASS_ORDER[0])  # type: ignore[arg-type]
    if not variants:
        return None
    for variant in variants:
        if variant["framing"] == "product_first":
            return variant
    return variants[0]


def _showcase_pick_shot_variants(
    *,
    template: str,
    age_segment: str | None,
    output_count: int,
    seed_key: str,
) -> list[tuple[ShotClass, ShotVariant]]:
    band = _resolve_pool_band(age_segment)
    pool = SHOT_POOL_BY_BAND.get(band, ADULT_POOL)
    template_pool = pool.get(template) or ADULT_POOL.get(template) or {}  # type: ignore[arg-type]
    plan = _shot_class_distribution(output_count)
    variants = _select_shot_variants(
        pool=template_pool,
        plan=plan,
        seed_key=seed_key,
        min_product_first=(
            output_count
            if output_count <= 4
            else 6
            if output_count <= 8
            else 12
        ),
    )
    return list(zip(plan, variants))


def _revision_prompt(
    *,
    instruction: str,
    product_analysis: dict[str, Any],
    selected_candidate: ModelCandidate,
) -> str:
    must_preserve = product_analysis.get("must_preserve")
    preserve = ", ".join(str(x) for x in must_preserve) if isinstance(must_preserve, list) else ""
    return (
        "请根据用户要求返修这张服饰电商模特图。"
        "【商品 1:1 还原】衣服以白底产品图为准，不要改款、改色、改廓形、改领口袖型衣长、改图案/logo、改纽扣拉链口袋缝线。"
        "保持已确认模特的人脸、发型、身材比例和整体身份不变。"
        "需要逐项保留的商品细节："
        f"{preserve or '颜色、版型、领口、袖型、长度、logo/图案、口袋、纽扣、缝线'}。"
        f"返修要求：{instruction}，仅按此改动，不动商品和模特身份。"
        f"参考模特方案：{selected_candidate.id}。"
    )


def _accessory_preview_prompt(
    *,
    accessory_plan: dict[str, Any],
    style_prompt: str,
    age_context: str = "",
) -> str:
    items = accessory_plan.get("items")
    item_list = _clean_string_list(
        (str(item) for item in items) if isinstance(items, list) else [],
        max_items=8,
        max_len=80,
    )
    item_text = "、".join(item_list)
    strength = str(accessory_plan.get("strength") or "subtle")
    enabled = bool(accessory_plan.get("enabled", True))
    accessory_line = (
        f"只添加这些配饰：{item_text}。不要自动新增未列出的包、帽子、腰带、眼镜、首饰、鞋子或道具。"
        if enabled and item_text
        else "不添加新配饰；保持参考图里的基础造型干净稳定。"
    )
    style = style_prompt.strip() or "干净高级的电商参考图，克制自然"
    age_direction = _accessory_age_direction(" ".join([age_context, style]).strip())
    return (
        "请根据上传的已确认模特四宫格参考图，生成一张新的白底模特配饰四宫格参考图。"
        "核心目标是在同一个模特、同一套基础中性服装上预览配饰效果，供后续商品融合图参考；"
        "不要生成最终商品穿搭图。"
        "画面必须保持 2x2 四宫格参考图，不要拆成多张图；"
        "四格内容固定为：正面全身、侧面全身、背面全身、近景头像；"
        "布局顺序为左上正面全身、右上侧面全身、左下背面全身、右下近景头像。"
        "每一格都用白底或近白底、同一摄影棚光线、清晰边界；"
        "不要文字标签、编号、边框标题或水印。"
        "严格保持参考图里的同一张脸、发型、肤色、年龄感、身高、身材比例、肢体长度、"
        "体态和基础服装；不要换人，不要美颜成网红脸，不要改成时装大片造型。"
        "模特只穿原参考图中的简单中性基础服装，不要穿商品图中的衣服，"
        "不要出现任何商品服饰、logo、图案或新衣服细节。"
        f"配饰要求：{accessory_line}"
        f"配饰强度：{_accessory_strength_direction(strength)}。"
        "配饰必须真实贴合身体和透视：耳饰在耳垂位置，项链贴合颈部，包带、腰带、鞋帽与姿态一致；"
        "不能漂浮、变形、穿模，不能遮挡脸、手、脚和身体轮廓。"
        "不要让配饰遮挡未来商品展示区域；不要添加多余道具、家具、背景场景或手持物，"
        "除非明确列在配饰里。"
        f"年龄与风格：{age_direction} "
        f"补充方向：{style}。"
        "输出风格：高质量真实商业摄影参考图，清晰、干净、可作为后续服饰电商生成的稳定参考。"
    )


def _accessory_plan_from_product_analysis(product_analysis: dict[str, Any] | None) -> dict[str, Any]:
    raw_items = (product_analysis or {}).get("styling_recommendations")
    items = _clean_string_list(_coerce_string_list(raw_items), max_items=3, max_len=80)
    return {
        "enabled": True,
        "items": items,
        "strength": "subtle",
    }


def _coerce_accessory_plan_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    enabled = bool(value.get("enabled", True))
    strength = str(value.get("strength") or "subtle")
    if strength not in {"subtle", "medium", "strong"}:
        strength = "subtle"
    items = value.get("items")
    return {
        "enabled": enabled,
        "items": _clean_string_list(
            (str(item) for item in items) if isinstance(items, list) else [],
            max_items=12,
            max_len=80,
        ),
        "strength": strength,
    }


def _quality_review_prompt(
    *,
    product_analysis: dict[str, Any],
    selected_candidate: ModelCandidate,
    shot_type: str | None,
) -> str:
    must_preserve = product_analysis.get("must_preserve")
    preserve = (
        ", ".join(str(x) for x in must_preserve)
        if isinstance(must_preserve, list)
        else "garment color, silhouette, neckline, sleeve shape, length, logo, pattern, buttons, pockets, zippers, seams"
    )
    brief = selected_candidate.model_brief_json or {}
    return (
        "请对生成的服饰电商模特图做自动质检。对比商品参考图、已确认模特参考图和最终图，检查："
        "1. 是否还是同一件商品（核对：颜色、廓形、领口袖型衣长、图案/logo/文字、纽扣拉链口袋缝线拼接）；"
        "任一关键细节不一致，product_fidelity_score 须低于 60、recommendation 须为 revise；"
        "2. 模特人脸、发型、身材比例和年龄感是否接近确认方案；"
        "3. 手、脚、脸、衣服边缘、背景是否有明显瑕疵；"
        "4. 是否适合作为电商主图使用；"
        "5. 是否单人照，多人出现要 revise。"
        "只返回严格 JSON，字段：overall_score, product_fidelity_score, model_consistency_score, "
        "aesthetic_score, artifact_score, issues, recommendation。分数 0-100，recommendation 只能是 approve 或 revise。"
        "issues 需列出商品不还原的具体点（如「图案位置改变」「领口变形」「颜色偏移」），便于返修定位。"
        f"必须保留：{preserve}。镜头类型：{shot_type or 'unknown'}。"
        f"已确认模特摘要：{brief.get('summary') or 'synthetic ecommerce model'}。"
    )


PRODUCT_ANALYSIS_FIELDS = {
    "category",
    "color",
    "material_guess",
    "silhouette",
    "key_details",
    "must_preserve",
    "risks",
    "styling_recommendations",
    "background_recommendation",
}


def _extract_jsonish_value(value: Any) -> Any:
    """Unwrap common model/API envelopes until a likely JSON payload is reached."""
    if isinstance(value, dict):
        for key in ("parsed", "json", "arguments", "content", "text", "output_text"):
            inner = value.get(key)
            if inner not in (None, ""):
                return _extract_jsonish_value(inner)
        if "output" in value:
            return _extract_jsonish_value(value["output"])
        return value
    if isinstance(value, list):
        if len(value) == 1:
            return _extract_jsonish_value(value[0])
        dict_items = [item for item in value if isinstance(item, dict)]
        if dict_items and all(
            any(key in item for key in ("type", "text", "content")) for item in dict_items
        ):
            chunks = [
                str(_extract_jsonish_value(item))
                for item in dict_items
                if _extract_jsonish_value(item) not in (None, "")
            ]
            return "\n".join(chunks)
    return value


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return _dedupe_nonempty(str(item) for item in value if item not in (None, ""))
    if isinstance(value, str):
        raw = value.strip()
        if not raw or raw.lower() == "unknown":
            return []
        return _dedupe_nonempty(re.split(r"[、,，;\n]+", raw))
    return []


def _normalize_product_analysis_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    payload = dict(parsed)
    alias_map = {
        "material": "material_guess",
        "details": "key_details",
        "preserve": "must_preserve",
        "must_keep": "must_preserve",
        "recommendations": "styling_recommendations",
        "accessories": "styling_recommendations",
        "background": "background_recommendation",
        "scene": "background_recommendation",
        "scene_recommendation": "background_recommendation",
    }
    for source, target in alias_map.items():
        if target not in payload and source in payload:
            payload[target] = payload[source]

    for key in ("key_details", "must_preserve", "risks", "styling_recommendations"):
        payload[key] = _coerce_string_list(payload.get(key))
    for key in ("category", "color", "material_guess", "silhouette"):
        value = payload.get(key)
        payload[key] = str(value).strip() if value not in (None, "") else "unknown"
    background = payload.get("background_recommendation")
    payload["background_recommendation"] = (
        str(background).strip() if background not in (None, "") else "unknown"
    )

    preserve = _coerce_string_list(payload.get("must_preserve"))
    if not preserve:
        visible_bits = [
            payload.get("color"),
            payload.get("silhouette"),
            *payload.get("key_details", []),
        ]
        preserve = _dedupe_nonempty(
            str(item)
            for item in visible_bits
            if item not in (None, "", "unknown")
        )
    payload["must_preserve"] = preserve or ["颜色", "廓形", "可见商品细节"]
    return {key: payload.get(key) for key in PRODUCT_ANALYSIS_FIELDS}


def _try_parse_json_text(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raw = "模型没有返回商品约束内容，请重新生成或手动修正。"
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.removeprefix("json").strip()
    try:
        value = _extract_jsonish_value(json.loads(raw))
        if isinstance(value, str) and value.strip() != raw:
            return _try_parse_json_text(value)
        if isinstance(value, dict):
            return _normalize_product_analysis_payload(value)
        return {"summary_text": value}
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                value = _extract_jsonish_value(json.loads(raw[start : end + 1]))
                if isinstance(value, str):
                    return _try_parse_json_text(value)
                if isinstance(value, dict):
                    return _normalize_product_analysis_payload(value)
                return {"summary_text": value}
            except json.JSONDecodeError:
                pass
    return {
        "category": "需人工复核",
        "color": "需人工复核",
        "material_guess": "需人工复核",
        "silhouette": "需人工复核",
        "key_details": [raw],
        "must_preserve": ["颜色", "廓形", "可见商品细节"],
        "styling_recommendations": [],
        "background_recommendation": "根据衣服风格选择干净高级的商业摄影氛围",
        "risks": ["模型没有返回结构化 JSON，请人工复核文本摘要"],
        "summary_text": raw,
    }


def _clamp_score(value: Any, default: int = 0) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        score = default
    return max(0, min(100, score))


def _quality_payload_from_text(text: str) -> dict[str, Any]:
    parsed = _try_parse_json_text(text)
    issues = parsed.get("issues")
    if not isinstance(issues, list):
        issues = [
            {
                "severity": "medium",
                "type": "quality_review",
                "message": str(parsed.get("summary_text") or text or "QC review did not return issue details."),
            }
        ]
    recommendation = str(parsed.get("recommendation") or "review").strip().lower()
    if recommendation not in {"approve", "revise"}:
        recommendation = "revise"
    return {
        "overall_score": _clamp_score(parsed.get("overall_score"), 70),
        "product_fidelity_score": _clamp_score(parsed.get("product_fidelity_score"), 70),
        "model_consistency_score": _clamp_score(parsed.get("model_consistency_score"), 70),
        "aesthetic_score": _clamp_score(parsed.get("aesthetic_score"), 70),
        "artifact_score": _clamp_score(parsed.get("artifact_score"), 70),
        "issues_json": [item for item in issues if isinstance(item, dict)] or [
            {
                "severity": "medium",
                "type": "quality_review",
                "message": "QC review returned no structured issues.",
            }
        ],
        "recommendation": recommendation,
    }


async def _create_workflow_task(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    intent: Intent,
    text: str,
    attachment_ids: list[str],
    idempotency_key: str,
    workflow_run_id: str,
    workflow_step_key: str,
    image_params: ImageParamsIn | None = None,
    chat_params: ChatParamsIn | None = None,
    workflow_meta: dict[str, Any] | None = None,
) -> tuple[_PublishBundle, str | None, list[str]]:
    user_msg = Message(
        conversation_id=conv.id,
        role=Role.USER.value,
        content={
            "text": text,
            "attachments": [{"image_id": image_id} for image_id in attachment_ids],
            "workflow_run_id": workflow_run_id,
            "workflow_step_key": workflow_step_key,
        },
        intent=None,
        status=None,
    )
    db.add(user_msg)
    await db.flush()

    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        account_mode=getattr(user, "account_mode", "wallet"),
        conv=conv,
        user_msg=user_msg,
        intent=intent,
        idempotency_key=idempotency_key[:64],
        image_params=image_params or ImageParamsIn(),
        chat_params=chat_params or ChatParamsIn(),
        system_prompt=None,
        attachment_ids=attachment_ids,
        text=text,
    )

    meta = {
        "workflow_run_id": workflow_run_id,
        "workflow_type": WORKFLOW_TYPE,
        "workflow_step_key": workflow_step_key,
        **(workflow_meta or {}),
    }
    if result.completion_id:
        comp = await db.get(Completion, result.completion_id)
        if comp is not None:
            req = dict(comp.upstream_request or {})
            req.update(meta)
            comp.upstream_request = req
    for generation_id in result.generation_ids:
        gen = await db.get(Generation, generation_id)
        if gen is not None:
            req = dict(gen.upstream_request or {})
            req.update(meta)
            gen.upstream_request = req

    bundle = _PublishBundle(
        assistant_msg_id=result.assistant_msg.id,
        message_ids=[user_msg.id, result.assistant_msg.id],
        outbox_payloads=result.outbox_payloads,
        outbox_rows=result.outbox_rows,
    )
    return bundle, result.completion_id, result.generation_ids


async def _publish_bundles(
    db: AsyncSession,
    *,
    user_id: str,
    conv_id: str,
    bundles: list[_PublishBundle],
) -> None:
    redis = get_redis()
    for bundle in bundles:
        await _publish_message_appended(
            redis=redis,
            user_id=user_id,
            conv_id=conv_id,
            message_ids=bundle.message_ids,
        )
        await _publish_assistant_task(
            db=db,
            redis=redis,
            user_id=user_id,
            conv_id=conv_id,
            assistant_msg_id=bundle.assistant_msg_id,
            outbox_payloads=bundle.outbox_payloads,
            outbox_rows=bundle.outbox_rows,
        )


def _fixed_size_for_quality(aspect_ratio: str, final_quality: str) -> str | None:
    if final_quality == "standard":
        return None
    high: dict[str, str] = {
        "1:1": "2048x2048",
        "4:5": "1600x2000",
        "3:4": "1536x2048",
        "4:3": "2048x1536",
        "16:9": "2560x1440",
        "9:16": "1440x2560",
        "3:2": "2016x1344",
        "2:3": "1344x2016",
        "21:9": "2688x1152",
        "9:21": "1152x2688",
    }
    four_k: dict[str, str] = {
        "1:1": "2880x2880",
        "4:5": "2560x3200",
        "3:4": "2448x3264",
        "4:3": "3264x2448",
        "16:9": "3840x2160",
        "9:16": "2160x3840",
        "3:2": "3504x2336",
        "2:3": "2336x3504",
        "21:9": "3808x1632",
        "9:21": "1632x3808",
    }
    return (four_k if final_quality == "4k" else high).get(aspect_ratio, high["4:5"])


def _image_params(
    *,
    aspect_ratio: str = "4:5",
    count: int = 1,
    render_quality: str = "high",
    final_quality: str | None = None,
    fast: bool = False,
) -> ImageParamsIn:
    fixed = _fixed_size_for_quality(aspect_ratio, final_quality or "high")
    return ImageParamsIn(
        aspect_ratio=aspect_ratio,  # type: ignore[arg-type]
        size_mode="fixed" if fixed else "auto",
        fixed_size=fixed,
        count=count,
        fast=fast,
        render_quality=render_quality,  # type: ignore[arg-type]
        output_format="jpeg",
        output_compression=100,
        background="opaque",
        moderation="low",
    )


def _candidate_image_params() -> ImageParamsIn:
    params = _image_params(
        aspect_ratio="4:5",
        count=1,
        render_quality="high",
        fast=False,
    )
    return params.model_copy(update={"output_format": "png", "output_compression": None})


def _accessory_preview_image_params() -> ImageParamsIn:
    params = _image_params(
        aspect_ratio="4:5",
        count=1,
        render_quality="high",
        final_quality="high",
        fast=False,
    )
    return params.model_copy(update={"output_format": "png", "output_compression": None})


def _merge_product_corrections(
    product_output: dict[str, Any],
    corrections: dict[str, Any],
) -> dict[str, Any]:
    final = dict(product_output or {})
    raw_corrections = corrections if isinstance(corrections, dict) else {}
    for key, value in raw_corrections.items():
        if value is not None:
            final[key] = value
    final["user_corrections"] = raw_corrections
    final["confirmed_at"] = _now().isoformat()
    return final


async def _sync_workflow_outputs(
    db: AsyncSession,
    run: WorkflowRun,
) -> None:
    steps = {step.step_key: step for step in await _load_steps(db, run.id)}

    product_step = steps.get("product_analysis")
    if product_step and product_step.status == "running" and product_step.task_ids:
        completion = (
            await db.execute(
                select(Completion)
                .where(Completion.id.in_(product_step.task_ids))
                .order_by(desc(Completion.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if completion is not None:
            if completion.status == CompletionStatus.SUCCEEDED.value:
                parsed = _try_parse_json_text(completion.text)
                product_step.output_json = parsed
                product_step.status = "needs_review"
                run.status = "needs_review"
                run.current_step = "product_analysis"
            elif completion.status == CompletionStatus.FAILED.value:
                product_step.status = "failed"
                product_step.output_json = {
                    "error_code": completion.error_code,
                    "error_message": completion.error_message,
                }
                run.status = "failed"

    candidates = list(
        (
            await db.execute(
                select(ModelCandidate)
                .where(ModelCandidate.workflow_run_id == run.id)
                .order_by(ModelCandidate.candidate_index.asc())
            )
        ).scalars().all()
    )
    if candidates:
        all_candidate_task_ids = [
            task_id for candidate in candidates for task_id in (candidate.task_ids or [])
        ]
        images_by_gen: dict[str, Image] = {}
        gens_by_id: dict[str, Generation] = {}
        bonus_gen_ids_by_parent: dict[str, list[str]] = {}
        bonus_parent_by_gen: dict[str, str] = {}
        if all_candidate_task_ids:
            base_generations = (
                await db.execute(
                    select(Generation).where(Generation.id.in_(all_candidate_task_ids))
                )
            ).scalars().all()
            bonus_generations = (
                await db.execute(
                    select(Generation)
                    .where(
                        Generation.user_id == run.user_id,
                        Generation.upstream_request["parent_generation_id"].astext.in_(
                            all_candidate_task_ids
                        ),
                        Generation.upstream_request["is_dual_race_bonus"]
                        .as_boolean()
                        .is_(True),
                    )
                    .order_by(Generation.created_at.asc(), Generation.id.asc())
                )
            ).scalars().all()
            generations = [*base_generations, *bonus_generations]
            gens_by_id = {g.id: g for g in generations}
            for generation in bonus_generations:
                req = generation.upstream_request or {}
                parent_id = req.get("parent_generation_id") if isinstance(req, dict) else None
                if isinstance(parent_id, str) and parent_id:
                    bonus_gen_ids_by_parent.setdefault(parent_id, []).append(generation.id)
                    bonus_parent_by_gen[generation.id] = parent_id
            images = (
                await db.execute(
                    select(Image)
                    .where(
                        Image.owner_generation_id.in_([g.id for g in generations]),
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            ).scalars().all()
            for image in images:
                if (
                    image.owner_generation_id
                    and image.owner_generation_id not in images_by_gen
                ):
                    images_by_gen[image.owner_generation_id] = image
        for candidate in candidates:
            candidate_image_ids: list[str] = []
            for task_id in candidate.task_ids or []:
                image = images_by_gen.get(task_id)
                if image is not None:
                    candidate_image_ids.append(image.id)
            candidate_image_ids = _dedupe_nonempty(candidate_image_ids)
            if candidate_image_ids:
                brief = dict(candidate.model_brief_json or {})
                brief["candidate_image_ids"] = candidate_image_ids
                candidate.model_brief_json = brief
            if candidate.contact_sheet_image_id is None:
                if candidate_image_ids:
                    candidate.contact_sheet_image_id = candidate_image_ids[0]
            if candidate.contact_sheet_image_id and candidate.status == "generating":
                candidate.status = "ready"
            elif (
                candidate.status == "generating"
                and candidate.task_ids
                and all(
                    gens_by_id.get(task_id) is not None
                    and gens_by_id[task_id].status == GenerationStatus.FAILED.value
                    for task_id in candidate.task_ids
                )
            ):
                candidate.status = "failed"

        existing_bonus_gen_ids = {
            task_id
            for candidate in candidates
            for task_id in (candidate.task_ids or [])
            if task_id in bonus_parent_by_gen
        }
        next_index = max((c.candidate_index for c in candidates), default=0) + 1
        for parent_task_id, bonus_gen_ids in bonus_gen_ids_by_parent.items():
            parent_candidate = next(
                (
                    candidate
                    for candidate in candidates
                    if parent_task_id in (candidate.task_ids or [])
                ),
                None,
            )
            if parent_candidate is None:
                continue
            for bonus_gen_id in bonus_gen_ids:
                if bonus_gen_id in existing_bonus_gen_ids:
                    continue
                bonus_image = images_by_gen.get(bonus_gen_id)
                if bonus_image is None:
                    continue
                brief = dict(parent_candidate.model_brief_json or {})
                brief["candidate_image_ids"] = [bonus_image.id]
                brief["source_candidate_id"] = parent_candidate.id
                brief["source_generation_id"] = parent_task_id
                brief["is_dual_race_bonus"] = True
                bonus_candidate = ModelCandidate(
                    workflow_run_id=run.id,
                    candidate_index=next_index,
                    status="ready",
                    contact_sheet_image_id=bonus_image.id,
                    model_brief_json=brief,
                    task_ids=[bonus_gen_id],
                )
                db.add(bonus_candidate)
                candidates.append(bonus_candidate)
                existing_bonus_gen_ids.add(bonus_gen_id)
                next_index += 1

        candidate_step = steps.get("model_candidates")
        if candidate_step and candidate_step.status == "running":
            ready_count = sum(1 for c in candidates if c.status == "ready")
            failed_count = sum(1 for c in candidates if c.status == "failed")
            if ready_count >= MODEL_CANDIDATE_COUNT:
                candidate_step.status = "needs_review"
                candidate_step.image_ids = _dedupe_nonempty(
                    image_id
                    for c in candidates
                    for image_id in (
                        (c.model_brief_json or {}).get("candidate_image_ids")
                        if isinstance(
                            (c.model_brief_json or {}).get("candidate_image_ids"), list
                        )
                        else [c.contact_sheet_image_id]
                    )
                    if isinstance(image_id, str)
                )
                run.current_step = "model_approval"
                run.status = "needs_review"
                approval_step = steps.get("model_approval")
                if approval_step and approval_step.status == "waiting_input":
                    approval_step.status = "needs_review"
            elif failed_count and failed_count == len(candidates):
                candidate_step.status = "failed"
                failed_generations = [
                    generation
                    for generation in gens_by_id.values()
                    if generation.status == GenerationStatus.FAILED.value
                ]
                output_json = dict(candidate_step.output_json or {})
                output_json["failed_generation_ids"] = [
                    generation.id for generation in failed_generations
                ]
                output_json["error_message"] = _task_error_summary(
                    failed_generations,
                    "模特候选生成失败",
                )
                candidate_step.output_json = output_json
                run.current_step = "model_candidates"
                run.status = "failed"

    showcase_step = steps.get("showcase_generation")
    quality_step = steps.get("quality_review")
    approval_step = steps.get("model_approval")
    if approval_step and approval_step.task_ids:
        accessory_base_generations = (
            await db.execute(
                select(Generation).where(Generation.id.in_(approval_step.task_ids))
            )
        ).scalars().all()
        accessory_bonus_generations = (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == run.user_id,
                    Generation.upstream_request["parent_generation_id"].astext.in_(
                        approval_step.task_ids
                    ),
                    Generation.upstream_request["is_dual_race_bonus"]
                    .as_boolean()
                    .is_(True),
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        ).scalars().all()
        accessory_generations = [
            *accessory_base_generations,
            *accessory_bonus_generations,
        ]
        accessory_images = (
            await db.execute(
                select(Image)
                .where(
                    Image.owner_generation_id.in_(
                        [generation.id for generation in accessory_generations]
                    ),
                    Image.deleted_at.is_(None),
                )
                .order_by(Image.created_at.asc(), Image.id.asc())
            )
        ).scalars().all()
        if accessory_images:
            approval_step.image_ids = _dedupe_nonempty(image.id for image in accessory_images)
            if approval_step.status == "running":
                approval_step.status = "needs_review"
                run.status = "needs_review"
                run.current_step = "model_approval"
        else:
            failed = [
                generation
                for generation in accessory_generations
                if generation.status == GenerationStatus.FAILED.value
            ]
            active = [
                generation
                for generation in accessory_generations
                if generation.status
                in {GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value}
            ]
            if approval_step.status == "running" and failed and not active:
                output_json = dict(approval_step.output_json or {})
                output_json["failed_generation_ids"] = [g.id for g in failed]
                output_json["error_message"] = _task_error_summary(
                    failed,
                    "配饰四宫格生成失败",
                )
                approval_step.output_json = output_json
                approval_step.status = "failed"
                run.status = "failed"
                run.current_step = "model_approval"
    if showcase_step and showcase_step.task_ids:
        base_generations = (
            await db.execute(
                select(Generation).where(Generation.id.in_(showcase_step.task_ids))
            )
        ).scalars().all()
        bonus_generations = (
            await db.execute(
                select(Generation)
                .where(
                    Generation.user_id == run.user_id,
                    Generation.upstream_request["parent_generation_id"].astext.in_(
                        showcase_step.task_ids
                    ),
                    Generation.upstream_request["is_dual_race_bonus"]
                    .as_boolean()
                    .is_(True),
                )
                .order_by(Generation.created_at.asc(), Generation.id.asc())
            )
        ).scalars().all()
        generations = [*base_generations, *bonus_generations]
        images = (
            await db.execute(
                select(Image)
                .where(
                    Image.owner_generation_id.in_([generation.id for generation in generations]),
                    Image.deleted_at.is_(None),
                )
                .order_by(Image.created_at.asc(), Image.id.asc())
            )
        ).scalars().all()
        image_ids = _dedupe_nonempty(image.id for image in images)
        if image_ids:
            showcase_step.image_ids = image_ids
        expected = _showcase_expected_image_count(
            showcase_input=showcase_step.input_json or {},
            fallback_task_count=len(showcase_step.task_ids),
        )
        succeeded = [
            generation
            for generation in generations
            if generation.status == GenerationStatus.SUCCEEDED.value
        ]
        active = [
            generation
            for generation in generations
            if generation.status
            in {GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value}
        ]
        failed = [
            generation
            for generation in generations
            if generation.status == GenerationStatus.FAILED.value
        ]
        has_enough_output_images = len(image_ids) >= expected
        if showcase_step.status in {"running", "failed"} and has_enough_output_images:
            showcase_step.status = "completed"
            if failed:
                output_json = dict(showcase_step.output_json or {})
                output_json["failed_generation_ids"] = [g.id for g in failed]
                output_json["succeeded_generation_ids"] = [g.id for g in succeeded]
                output_json["error_message"] = _task_error_summary(
                    failed,
                    "部分展示图生成失败",
                )
                output_json["recovered_by_bonus_images"] = True
                showcase_step.output_json = output_json
            if quality_step:
                quality_step.status = "needs_review"
                quality_step.image_ids = image_ids
                reports = await _load_quality_reports(db, run.id)
                quality_step.output_json = _merge_quality_summary_payload(
                    quality_step.output_json,
                    reports,
                )
                run.current_step = "quality_review"
            else:
                run.current_step = "showcase_generation"
            run.status = "needs_review"
        elif showcase_step.status == "running" and failed and not active:
            showcase_step.status = "failed"
            showcase_step.output_json = {
                "failed_generation_ids": [g.id for g in failed],
                "succeeded_generation_ids": [g.id for g in succeeded],
                "error_message": _task_error_summary(
                    failed,
                    "展示图生成失败",
                ),
            }
            run.status = "failed"
        elif showcase_step.status == "completed" and quality_step:
            quality_step.image_ids = image_ids
            await _sync_quality_reports_from_tasks(
                db,
                run=run,
                quality_step=quality_step,
            )
            reports = await _load_quality_reports(db, run.id)
            if (
                image_ids
                and len(reports) >= len(image_ids)
                and quality_step.status == "running"
            ):
                quality_step.status = "needs_review"
                run.status = "needs_review"
            quality_step.output_json = _merge_quality_summary_payload(
                quality_step.output_json,
                reports,
            )
            if (
                image_ids
                and quality_step.status in {"waiting_input", "running", "needs_review"}
                and run.status != "completed"
                and run.current_step == "showcase_generation"
            ):
                quality_step.status = "needs_review"
                run.current_step = "quality_review"
                run.status = "needs_review"


def _quality_summary_payload(reports: list[QualityReport]) -> dict[str, Any]:
    if not reports:
        return {"overall": "pending", "image_count": 0}
    revise_count = sum(1 for report in reports if report.recommendation == "revise")
    return {
        "overall": "revise" if revise_count else "approve",
        "image_count": len(reports),
        "revise_count": revise_count,
        "average_score": round(
            sum(report.overall_score for report in reports) / max(1, len(reports)),
            1,
        ),
    }


def _merge_quality_summary_payload(
    current: dict[str, Any] | None,
    reports: list[QualityReport],
) -> dict[str, Any]:
    payload = dict(current or {})
    payload.update(_quality_summary_payload(reports))
    review_tasks = (current or {}).get("review_tasks")
    if isinstance(review_tasks, dict):
        payload["review_tasks"] = review_tasks
        payload["review_task_count"] = len(review_tasks)
    return payload


async def _load_quality_reports(db: AsyncSession, run_id: str) -> list[QualityReport]:
    return list(
        (
            await db.execute(
                select(QualityReport)
                .where(QualityReport.workflow_run_id == run_id)
                .order_by(QualityReport.created_at.asc(), QualityReport.id.asc())
            )
        ).scalars().all()
    )


async def _sync_quality_reports_from_tasks(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    quality_step: WorkflowStep,
) -> None:
    output_json = dict(quality_step.output_json or {})
    review_map = output_json.get("review_tasks")
    if not isinstance(review_map, dict) or not review_map:
        return
    existing_by_image = {
        image_id: report
        for image_id, report in (
            (
                report.image_id,
                report,
            )
            for report in await _load_quality_reports(db, run.id)
        )
    }
    task_ids = [
        task_id
        for task_id in review_map.values()
        if isinstance(task_id, str) and task_id
    ]
    if not task_ids:
        return
    completions = (
        await db.execute(
            select(Completion).where(
                Completion.id.in_(task_ids),
                Completion.user_id == run.user_id,
            )
        )
    ).scalars().all()
    completion_by_id = {completion.id: completion for completion in completions}
    for image_id, raw_task_id in review_map.items():
        if not isinstance(image_id, str) or not isinstance(raw_task_id, str):
            continue
        completion = completion_by_id.get(raw_task_id)
        if completion is None:
            continue
        if completion.status == CompletionStatus.SUCCEEDED.value:
            payload = _quality_payload_from_text(completion.text)
        elif completion.status == CompletionStatus.FAILED.value:
            payload = {
                "overall_score": 0,
                "product_fidelity_score": 0,
                "model_consistency_score": 0,
                "aesthetic_score": 0,
                "artifact_score": 0,
                "issues_json": [
                    {
                        "severity": "high",
                        "type": "quality_review_failed",
                        "message": completion.error_message
                        or "Automatic quality review failed; revise or rerun before delivery.",
                    }
                ],
                "recommendation": "revise",
            }
        else:
            continue
        existing = existing_by_image.get(image_id)
        if existing is None:
            db.add(
                QualityReport(
                    workflow_run_id=run.id,
                    image_id=image_id,
                    **payload,
                )
            )
        else:
            existing.overall_score = payload["overall_score"]
            existing.product_fidelity_score = payload["product_fidelity_score"]
            existing.model_consistency_score = payload["model_consistency_score"]
            existing.aesthetic_score = payload["aesthetic_score"]
            existing.artifact_score = payload["artifact_score"]
            existing.issues_json = payload["issues_json"]
            existing.recommendation = payload["recommendation"]


async def _ensure_quality_review_tasks(
    db: AsyncSession,
    *,
    user: User,
    conv: Conversation,
    run: WorkflowRun,
    showcase_step: WorkflowStep,
    quality_step: WorkflowStep,
) -> list[_PublishBundle]:
    """Automatic quality review is disabled for apparel workflows."""
    return []


async def _ensure_legacy_quality_reports(
    db: AsyncSession,
    *,
    run_id: str,
    images: list[Image],
) -> None:
    """Retained for migrations/manual recovery; normal path uses review tasks."""
    if not images:
        return
    existing = {
        image_id
        for image_id in (
            await db.execute(
                select(QualityReport.image_id).where(
                    QualityReport.workflow_run_id == run_id,
                    QualityReport.image_id.in_([image.id for image in images]),
                )
            )
        ).scalars().all()
    }
    for image in images:
        if image.id in existing:
            continue
        report = QualityReport(
            workflow_run_id=run_id,
            image_id=image.id,
            overall_score=86,
            product_fidelity_score=84,
            model_consistency_score=86,
            aesthetic_score=88,
            artifact_score=86,
            issues_json=[
                {
                    "severity": "low",
                    "type": "automatic_quality_review",
                    "message": "Automatic QC completed. Review garment color, structure, model identity, and artifacts before final delivery.",
                }
            ],
            recommendation="approve",
        )
        db.add(report)


def _next_action_for(run: WorkflowRun) -> str:
    if run.status == "completed":
        return "查看交付"
    return {
        "product_analysis": "确认商品约束",
        "model_settings": "生成模特候选",
        "model_candidates": "等待模特候选",
        "model_approval": "确认模特",
        "showcase_generation": "开始生成展示图",
        "quality_review": "查看质检",
        "delivery": "下载最终图",
    }.get(run.current_step, "继续项目")


async def _image_out_map(db: AsyncSession, images: list[Image]) -> dict[str, ImageOut]:
    if not images:
        return {}
    variant_rows = (
        await db.execute(
            select(ImageVariant.image_id, ImageVariant.kind).where(
                ImageVariant.image_id.in_([image.id for image in images])
            )
        )
    ).all()
    variant_map: dict[str, set[str]] = {}
    for image_id, kind in variant_rows:
        variant_map.setdefault(image_id, set()).add(kind)
    return {image.id: _image_to_out(image, variant_map.get(image.id)) for image in images}


def _image_to_out(img: Image, variant_kinds: set[str] | None = None) -> ImageOut:
    variant_kinds = variant_kinds or set()
    metadata = img.metadata_jsonb if isinstance(img.metadata_jsonb, dict) else {}
    billing_label = (
        metadata.get("billing_label")
        if isinstance(metadata.get("billing_label"), str)
        else None
    )
    billing_exempt_reason = (
        metadata.get("billing_exempt_reason")
        if isinstance(metadata.get("billing_exempt_reason"), str)
        else None
    )
    is_dual_race_bonus = metadata.get("is_dual_race_bonus") is True
    billing_free = (
        metadata.get("billing_free") is True
        or is_dual_race_bonus
        or billing_label == "free"
    )
    return ImageOut(
        id=img.id,
        source=img.source,
        parent_image_id=img.parent_image_id,
        owner_generation_id=img.owner_generation_id,
        width=img.width,
        height=img.height,
        mime=img.mime,
        blurhash=img.blurhash,
        url=f"/api/images/{img.id}/binary",
        display_url=f"/api/images/{img.id}/variants/display2048",
        preview_url=(
            f"/api/images/{img.id}/variants/preview1024"
            if "preview1024" in variant_kinds
            else None
        ),
        thumb_url=(
            f"/api/images/{img.id}/variants/thumb256"
            if "thumb256" in variant_kinds
            else None
        ),
        metadata_jsonb=metadata,
        is_dual_race_bonus=is_dual_race_bonus,
        billing_free=billing_free,
        billing_label=billing_label,
        billing_exempt_reason=billing_exempt_reason,
    )


async def _build_run_out(db: AsyncSession, run: WorkflowRun) -> WorkflowRunOut:
    await _sync_workflow_outputs(db, run)
    await db.flush()
    await db.refresh(run)

    steps = await _load_steps(db, run.id)
    candidates = list(
        (
            await db.execute(
                select(ModelCandidate)
                .where(ModelCandidate.workflow_run_id == run.id)
                .order_by(ModelCandidate.candidate_index.asc())
            )
        ).scalars().all()
    )
    reports = await _load_quality_reports(db, run.id)
    for row in [*steps, *candidates, *reports]:
        await db.refresh(row)

    # 先拉海报相关行；poster_masters/renders 的 image_id 和 task_ids 要
    # 加入下面 owned_images / generations 的扫描集合。
    poster_masters_rows: list[PosterMaster] = []
    poster_renders_rows: list[PosterRender] = []
    if run.type == POSTER_WORKFLOW_TYPE:
        await _sync_poster_workflow_outputs(db, run)
        await db.flush()
        poster_masters_rows = list(
            (
                await db.execute(
                    select(PosterMaster)
                    .where(PosterMaster.workflow_run_id == run.id)
                    .order_by(PosterMaster.candidate_index.asc())
                )
            ).scalars().all()
        )
        poster_renders_rows = list(
            (
                await db.execute(
                    select(PosterRender)
                    .where(PosterRender.workflow_run_id == run.id)
                    .order_by(PosterRender.created_at.asc(), PosterRender.id.asc())
                )
            ).scalars().all()
        )
        for row in [*poster_masters_rows, *poster_renders_rows]:
            await db.refresh(row)

    all_task_ids: set[str] = set()
    image_ids: set[str] = set(run.product_image_ids or [])
    for step in steps:
        all_task_ids.update(step.task_ids or [])
        image_ids.update(step.image_ids or [])
    for candidate in candidates:
        all_task_ids.update(candidate.task_ids or [])
        image_ids.update(_candidate_reference_image_ids(candidate))
    for report in reports:
        image_ids.add(report.image_id)
    for master in poster_masters_rows:
        all_task_ids.update(master.task_ids or [])
        if master.image_id:
            image_ids.add(master.image_id)
    for render in poster_renders_rows:
        all_task_ids.update(render.task_ids or [])
        if render.image_id:
            image_ids.add(render.image_id)

    generations: list[Generation] = []
    if all_task_ids:
        generations = list(
            (
                await db.execute(
                    select(Generation)
                    .where(Generation.id.in_(all_task_ids), Generation.user_id == run.user_id)
                    .order_by(Generation.created_at.asc(), Generation.id.asc())
                )
            ).scalars().all()
        )
    if all_task_ids:
        owned_images = list(
            (
                await db.execute(
                    select(Image)
                    .where(
                        or_(
                            Image.id.in_(image_ids) if image_ids else Image.id == "__none__",
                            Image.owner_generation_id.in_(all_task_ids),
                        ),
                        Image.user_id == run.user_id,
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            ).scalars().all()
        )
    elif image_ids:
        owned_images = list(
            (
                await db.execute(
                    select(Image)
                    .where(
                        Image.id.in_(image_ids),
                        Image.user_id == run.user_id,
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            ).scalars().all()
        )
    else:
        owned_images = []
    for row in [*generations, *owned_images]:
        await db.refresh(row)

    image_map = await _image_out_map(db, owned_images)
    product_image_ids = set(run.product_image_ids or [])
    product_images = [
        image_map[iid] for iid in (run.product_image_ids or []) if iid in image_map
    ]
    generated_images = [
        image_map[image.id]
        for image in owned_images
        # 项目内的“非商品图”要都能被前端按 id 找到：
        # 包括候选图、展示图，以及从模特库选入并 materialize 到当前用户空间的参考图。
        if image.id not in product_image_ids and image.id in image_map
    ]

    poster_masters_out = [PosterMasterOut.model_validate(m) for m in poster_masters_rows]
    poster_renders_out = [PosterRenderOut.model_validate(r) for r in poster_renders_rows]

    return WorkflowRunOut(
        id=run.id,
        conversation_id=run.conversation_id,
        user_id=run.user_id,
        type=run.type,
        status=run.status,
        title=run.title,
        user_prompt=run.user_prompt,
        product_image_ids=run.product_image_ids or [],
        current_step=run.current_step,
        quality_mode=run.quality_mode,
        metadata_jsonb=run.metadata_jsonb or {},
        created_at=run.created_at,
        updated_at=run.updated_at,
        steps=[WorkflowStepOut.model_validate(step) for step in steps],
        model_candidates=[ModelCandidateOut.model_validate(c) for c in candidates],
        quality_reports=[QualityReportOut.model_validate(r) for r in reports],
        poster_masters=poster_masters_out,
        poster_renders=poster_renders_out,
        product_images=product_images,
        generated_images=generated_images,
        generations=[GenerationOut.model_validate(g) for g in generations],
    )


def _list_item_from_run(run: WorkflowRun, output_count: int = 0) -> WorkflowRunListItemOut:
    return WorkflowRunListItemOut(
        id=run.id,
        conversation_id=run.conversation_id,
        type=run.type,
        status=run.status,
        title=run.title,
        user_prompt=run.user_prompt,
        product_image_ids=run.product_image_ids or [],
        current_step=run.current_step,
        quality_mode=run.quality_mode,
        metadata_jsonb=run.metadata_jsonb or {},
        created_at=run.created_at,
        updated_at=run.updated_at,
        output_count=output_count,
        next_action=_next_action_for(run),
    )


@router.post(
    "/apparel-model-showcase",
    response_model=ApparelWorkflowCreateOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_apparel_model_showcase(
    body: ApparelWorkflowCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelWorkflowCreateOut:
    image_ids = await _validate_owned_images(
        db,
        user_id=user.id,
        image_ids=body.product_image_ids,
        min_count=1,
        max_count=3,
    )
    title = (body.title or "").strip() or "服饰模特展示图"
    conv = await _get_or_create_workflow_conversation(
        db,
        user=user,
        # Workflow task messages need a backing conversation, but it should not
        # attach to a user-visible chat session.
        conversation_id=None,
        title=title,
    )
    conv.title = title
    conv.archived = True
    run = WorkflowRun(
        conversation_id=conv.id,
        user_id=user.id,
        type=WORKFLOW_TYPE,
        status="running",
        title=title,
        user_prompt=body.user_prompt,
        product_image_ids=image_ids,
        current_step="product_analysis",
        quality_mode=body.quality_mode,
        metadata_jsonb={
            "template": WORKFLOW_TYPE,
            "mvp_scope": "adult_daily_apparel",
            "priority": ["model_consistency", "product_fidelity", "premium_aesthetic"],
            "model_profile": _metadata_model_profile_from_prompt(body.user_prompt),
        },
    )
    db.add(run)
    await db.flush()
    for step in _seed_steps(run, user_prompt=body.user_prompt):
        db.add(step)
    product_step = await _step(db, run.id, "product_analysis")

    bundle, completion_id, _ = await _create_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.VISION_QA,
        text=_product_analysis_prompt(body.user_prompt),
        attachment_ids=image_ids,
        idempotency_key=f"wf:{run.id}:analysis",
        workflow_run_id=run.id,
        workflow_step_key="product_analysis",
        chat_params=ChatParamsIn(reasoning_effort="low", stream=True),
        workflow_meta={"workflow_action": "product_analysis"},
    )
    product_step.task_ids = [completion_id] if completion_id else []
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    return ApparelWorkflowCreateOut(
        workflow_run_id=run.id,
        status=run.status,
        current_step=run.current_step,
    )


@router.get("/apparel-model-library", response_model=ApparelModelLibraryListOut)
async def list_apparel_model_library(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    age_segment: AgeSegment = Query(default="all"),
    source: str = Query(default="all"),
    appearance: str = Query(default="all"),
    q: str = Query(default=""),
) -> ApparelModelLibraryListOut:
    source = source.strip() or "all"
    if source not in MODEL_LIBRARY_SOURCES:
        raise _http("invalid_source", "invalid model library source", 422)
    age = str(age_segment)
    if age not in MODEL_LIBRARY_AGE_SEGMENTS:
        raise _http("invalid_age_segment", "invalid model library age segment", 422)
    appearance = appearance.strip() or "all"
    if appearance not in MODEL_LIBRARY_APPEARANCES:
        raise _http("invalid_appearance", "invalid model library appearance", 422)
    combined_items, migrated_legacy = await _combined_library_items(db, user.id)
    items = _filter_library_items(
        combined_items,
        source=source,
        age_segment=age,
        appearance=appearance,
        q=q,
    )
    if migrated_legacy:
        await db.commit()
    return ApparelModelLibraryListOut(
        items=[_model_library_item_out(item) for item in items],
        sync=_sync_state_out(user),
    )


@router.post(
    "/apparel-model-library/sync-presets",
    response_model=ApparelModelLibrarySyncOut,
    dependencies=[Depends(verify_csrf)],
)
async def sync_apparel_model_library_presets(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelModelLibrarySyncOut:
    if not _can_sync_library(user):
        raise _http("forbidden", "model library preset sync is not allowed", 403)
    _, proxy_url = await _resolve_model_library_sync_proxy(db)
    # cooldown / 并发控制全部在 _sync_library_presets_from_github_folder 内部处理
    return await _sync_library_presets_from_github_folder(
        _github_contents_url(),
        proxy_url=proxy_url,
    )


@router.get("/apparel-model-library/items/{item_id:path}/binary")
async def get_apparel_model_library_item_binary(
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    item = await _find_library_item(db, user_id=user.id, item_id=item_id)
    if item is None:
        raise _http("not_found", "model library item not found", 404)
    if item.get("image_id"):
        raise _http("use_image_api", "user library image is served by image API", 400)
    storage_key = str(item.get("image_storage_key") or "").strip()
    return _library_binary_response(storage_key, request)


@router.get("/apparel-model-library/items/{item_id:path}/thumb")
async def get_apparel_model_library_item_thumb(
    item_id: str,
    request: Request,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    item = await _find_library_item(db, user_id=user.id, item_id=item_id)
    if item is None:
        raise _http("not_found", "model library item not found", 404)
    if item.get("image_id"):
        raise _http("use_image_api", "user library image is served by image API", 400)
    storage_key = str(
        item.get("thumb_storage_key") or item.get("image_storage_key") or ""
    ).strip()
    return _library_binary_response(storage_key, request)


@router.post(
    "/apparel-model-library/items",
    response_model=ApparelModelLibraryItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_apparel_model_library_item(
    body: ApparelModelLibraryItemCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> ApparelModelLibraryItemOut:
    item = await _add_user_library_item(
        db,
        user_id=user.id,
        source=body.source,
        image_id=body.image_id,
        title=body.title,
        age_segment=body.age_segment,
        gender=body.gender,
        appearance_direction=body.appearance_direction,
        style_tags=body.style_tags,
    )
    await db.commit()
    item_id = str(item.get("id") or "")
    if body.auto_tag and item_id:
        background_tasks.add_task(_run_auto_tag_in_background, user.id, item_id)
    return _model_library_item_out(item)


@router.patch(
    "/apparel-model-library/items/{item_id:path}",
    response_model=ApparelModelLibraryItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_apparel_model_library_item(
    item_id: str,
    body: ApparelModelLibraryItemPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelModelLibraryItemOut:
    await _ensure_legacy_user_library_migrated(db, user.id)
    row = (
        await db.execute(
            select(ModelLibraryItem).where(
                ModelLibraryItem.id == item_id,
                ModelLibraryItem.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("not_found", "model library item not found", 404)
    if body.title is not None:
        row.title = body.title.strip()[:120]
    if body.age_segment is not None:
        row.age_segment = _normalize_age_segment(body.age_segment)
        row.library_folder = _model_library_folder_for_age(
            row.age_segment, row.gender
        )
    if body.gender is not None:
        row.gender = _clean_optional_text(body.gender, max_len=40)
        row.library_folder = _model_library_folder_for_age(
            row.age_segment, row.gender
        )
    if body.appearance_direction is not None:
        row.appearance_direction = _clean_optional_text(
            body.appearance_direction, max_len=80
        )
    if body.style_tags is not None:
        row.style_tags = _clean_style_tags(body.style_tags)
    await db.commit()
    await db.refresh(row)
    return _model_library_item_out(_model_library_row_to_dict(row))


async def _delete_apparel_model_library_item_for_user(
    db: AsyncSession,
    *,
    user_id: str,
    item_id: str,
) -> bool:
    """Delete a private item or hide a global preset for one user."""
    if item_id.startswith("user:"):
        removed_legacy = _remove_user_library_item_from_legacy_index(user_id, item_id)
        row = (
            await db.execute(
                select(ModelLibraryItem).where(
                    ModelLibraryItem.id == item_id,
                    ModelLibraryItem.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return removed_legacy
        await db.delete(row)
        return True

    item = await _find_library_item(db, user_id=user_id, item_id=item_id)
    if item is None or item.get("source") != "preset":
        return False
    existing = (
        await db.execute(
            select(ModelLibraryHiddenPreset).where(
                ModelLibraryHiddenPreset.user_id == user_id,
                ModelLibraryHiddenPreset.preset_id == item_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(ModelLibraryHiddenPreset(user_id=user_id, preset_id=item_id))
    _hide_preset_in_legacy_user_library_index(user_id, item_id)
    return True


@router.delete(
    "/apparel-model-library/items/{item_id:path}",
    dependencies=[Depends(verify_csrf)],
)
async def delete_apparel_model_library_item(
    item_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    await _ensure_legacy_user_library_migrated(db, user.id)
    deleted = await _delete_apparel_model_library_item_for_user(
        db,
        user_id=user.id,
        item_id=item_id,
    )
    if not deleted:
        raise _http("not_found", "model library item not found", 404)
    await db.commit()
    return {"ok": True}


@router.post(
    "/apparel-model-library/items/batch-delete",
    response_model=ApparelModelLibraryBatchDeleteOut,
    dependencies=[Depends(verify_csrf)],
)
async def batch_delete_apparel_model_library_items(
    body: ApparelModelLibraryBatchDeleteIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelModelLibraryBatchDeleteOut:
    await _ensure_legacy_user_library_migrated(db, user.id)
    item_ids = _dedupe_nonempty(body.item_ids)
    deleted = 0
    not_found: list[str] = []
    for item_id in item_ids:
        if await _delete_apparel_model_library_item_for_user(
            db,
            user_id=user.id,
            item_id=item_id,
        ):
            deleted += 1
        else:
            not_found.append(item_id)
    await db.commit()
    return ApparelModelLibraryBatchDeleteOut(deleted=deleted, not_found=not_found)


@router.get("", response_model=WorkflowRunListOut)
async def list_workflows(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    type: str | None = Query(default=None),  # noqa: A002 - API field name
    limit: int = Query(default=50, ge=1, le=100),
) -> WorkflowRunListOut:
    stmt = select(WorkflowRun).where(
        WorkflowRun.user_id == user.id,
        WorkflowRun.deleted_at.is_(None),
    )
    if type:
        stmt = stmt.where(WorkflowRun.type == type)
    else:
        # ProjectsIndex 默认隐藏独立库生成 workflow——它们是后台任务实体，
        # 不是用户感知的"项目"。调用方明确传 type 才会返回。
        stmt = stmt.where(WorkflowRun.type.notin_(HIDDEN_PROJECT_WORKFLOW_TYPES))
    runs = list(
        (
            await db.execute(
                stmt.order_by(desc(WorkflowRun.updated_at), desc(WorkflowRun.id)).limit(limit)
            )
        ).scalars().all()
    )
    output_counts: dict[str, int] = {}
    if runs:
        rows = (
            await db.execute(
                select(WorkflowStep.workflow_run_id, WorkflowStep.image_ids)
                .where(
                    WorkflowStep.workflow_run_id.in_([run.id for run in runs]),
                    WorkflowStep.step_key.in_(
                        ["showcase_generation", "multi_size_generation"]
                    ),
                )
            )
        ).all()
        for run_id, image_ids in rows:
            output_counts[run_id] = output_counts.get(run_id, 0) + len(image_ids or [])
    return WorkflowRunListOut(
        items=[_list_item_from_run(run, output_counts.get(run.id, 0)) for run in runs],
        next_cursor=None,
    )


@router.get("/{workflow_run_id}", response_model=WorkflowRunOut)
async def get_workflow(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.patch(
    "/{workflow_run_id}",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def patch_workflow(
    workflow_run_id: str,
    body: WorkflowRunPatchIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise _http("invalid_title", "title cannot be empty", 422)
        run.title = title
        if run.conversation_id:
            conv = (
                await db.execute(
                    select(Conversation).where(
                        Conversation.id == run.conversation_id,
                        Conversation.user_id == user.id,
                        Conversation.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if conv is not None:
                conv.title = title
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.delete(
    "/{workflow_run_id}",
    dependencies=[Depends(verify_csrf)],
)
async def delete_workflow(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    deleted_at = _now()
    await _soft_delete_workflow_generated_images(
        db,
        run=run,
        deleted_at=deleted_at,
        cancel_message="workflow deleted",
    )
    run.deleted_at = deleted_at
    if run.conversation_id:
        conv = (
            await db.execute(
                select(Conversation).where(
                    Conversation.id == run.conversation_id,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if conv is not None:
            conv.deleted_at = deleted_at
    await db.commit()
    return {"ok": True}


@router.post(
    "/{workflow_run_id}/steps/product-analysis/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_product_analysis(
    workflow_run_id: str,
    body: ProductAnalysisApproveIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    if product_step.status not in {"needs_review", "approved"}:
        raise _http("step_not_ready", "product analysis is not ready to approve", 409)
    product_step.output_json = _merge_product_corrections(
        product_step.output_json or {},
        body.corrections or {},
    )
    product_step.status = "approved"
    product_step.approved_at = _now()
    product_step.approved_by = user.id
    model_settings = await _step(db, run.id, "model_settings")
    if model_settings.status == "waiting_input":
        model_settings.status = "needs_review"
        model_settings.input_json = {
            "style_prompt": run.user_prompt,
            "avoid": ["过度网红感", "夸张姿势", "强烈妆容"],
        }
    run.current_step = "model_settings"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_model_candidates(
    workflow_run_id: str,
    body: ModelCandidatesCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    if product_step.status != "approved":
        raise _http("product_not_approved", "approve product analysis first", 409)
    existing_candidates = (
        await db.execute(
            select(ModelCandidate)
            .where(ModelCandidate.workflow_run_id == run.id)
            .order_by(ModelCandidate.candidate_index.asc())
        )
    ).scalars().all()

    model_settings = await _step(db, run.id, "model_settings")
    candidate_step = await _step(db, run.id, "model_candidates")
    if candidate_step.status == "running":
        raise _http("already_running", "model candidates are already being generated", 409)
    if any(candidate.status == "selected" for candidate in existing_candidates):
        raise _http(
            "model_already_selected",
            "reopen model selection before generating new candidates",
            409,
        )
    model_settings.status = "approved"
    model_settings.approved_at = _now()
    model_settings.approved_by = user.id
    model_settings.output_json = {
        "style_prompt": body.style_prompt or run.user_prompt,
        "avoid": body.avoid,
        "candidate_count": body.candidate_count,
        "accessory_plan": body.accessory_plan.model_dump(),
    }
    candidate_step.status = "running"
    candidate_step.input_json = model_settings.output_json
    run.current_step = "model_candidates"
    run.status = "running"

    conv = await _get_owned_conversation(db, user_id=user.id, conversation_id=run.conversation_id or "")
    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    model_direction = body.style_prompt or run.user_prompt or "premium ecommerce synthetic model"
    height_cm = _infer_model_height_cm(model_direction)
    height_requirement = _height_requirement(model_direction)
    existing_count = len(existing_candidates)
    for idx in range(1, body.candidate_count + 1):
        candidate_index = existing_count + idx
        candidate = ModelCandidate(
            workflow_run_id=run.id,
            candidate_index=candidate_index,
            status="generating",
            model_brief_json={
                "summary": model_direction,
                "candidate_index": candidate_index,
                "height_cm": height_cm,
                "height_label": f"身高 {height_cm}cm",
                "height_requirement": height_requirement,
                "product_context": product_step.output_json,
                "note": "未试穿商品，仅用于确认模特形象",
            },
        )
        db.add(candidate)
        await db.flush()
        bundle, _, gen_ids = await _create_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=Intent.TEXT_TO_IMAGE,
            text=_candidate_prompt(
                style_prompt=body.style_prompt or run.user_prompt,
                product_analysis=product_step.output_json or {},
                candidate_index=candidate_index,
                avoid=body.avoid,
            ),
            attachment_ids=[],
            idempotency_key=f"wf:{run.id[:24]}:cand:{candidate_index}",
            workflow_run_id=run.id,
            workflow_step_key="model_candidates",
            image_params=_candidate_image_params(),
            workflow_meta={
                "workflow_action": "model_candidate",
                "workflow_candidate_id": candidate.id,
                "workflow_candidate_index": candidate_index,
                "workflow_candidate_view": "concept_sheet",
            },
        )
        candidate.task_ids = gen_ids
        task_ids.extend(gen_ids)
        bundles.append(bundle)
    candidate_step.task_ids = task_ids
    approval = await _step(db, run.id, "model_approval")
    approval.input_json = {
        **(approval.input_json or {}),
        "accessory_plan": body.accessory_plan.model_dump(),
        "style_prompt": body.style_prompt or run.user_prompt,
    }
    if body.accessory_plan.enabled:
        approval.status = "waiting_input"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-library/select",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def select_apparel_model_library_item(
    workflow_run_id: str,
    body: ApparelModelLibrarySelectIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    if product_step.status != "approved":
        raise _http("product_not_approved", "approve product analysis first", 409)
    item = await _find_library_item(db, user_id=user.id, item_id=body.library_item_id)
    if item is None:
        raise _http("not_found", "model library item not found", 404)
    try:
        if item.get("source") == "preset":
            image = await _create_user_image_from_preset(db, user_id=user.id, item=item)
        else:
            image_id = str(item.get("image_id") or "").strip()
            image = await _owned_image(db, user_id=user.id, image_id=image_id)
    except HTTPException:
        # 已是结构化错误（404/400/...），让 _get_run 的 row lock 在事务回滚时自动释放
        await db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        logger.exception("select_apparel_model_library_item: image materialize failed")
        raise _http(
            "library_image_failed",
            f"failed to materialize library image: {exc}",
            500,
        ) from exc

    model_settings = await _step(db, run.id, "model_settings")
    now = _now()
    requested_accessory_plan = (
        body.accessory_plan.model_dump() if body.accessory_plan is not None else None
    )
    existing_accessory_plan = _coerce_accessory_plan_payload(
        (model_settings.output_json or {}).get("accessory_plan")
    ) or _coerce_accessory_plan_payload((model_settings.input_json or {}).get("accessory_plan"))
    accessory_plan = (
        requested_accessory_plan
        or existing_accessory_plan
        or _accessory_plan_from_product_analysis(product_step.output_json or {})
    )
    style_prompt = (
        body.style_prompt.strip()
        or str((model_settings.output_json or {}).get("style_prompt") or "").strip()
        or str((model_settings.input_json or {}).get("style_prompt") or "").strip()
        or run.user_prompt
    )
    existing_count = (
        await db.execute(
            select(ModelCandidate.id).where(ModelCandidate.workflow_run_id == run.id)
        )
    ).scalars().all()
    candidate = ModelCandidate(
        workflow_run_id=run.id,
        candidate_index=len(existing_count) + 1,
        contact_sheet_image_id=image.id,
        portrait_image_id=image.id,
        status="ready",
        selected_at=None,
        model_brief_json={
            "summary": item.get("title") or "库内模特",
            "source": "model_library",
            "library_item_id": body.library_item_id,
            "age_segment": _normalize_age_segment(item.get("age_segment")),
            "gender": item.get("gender"),
            "appearance_direction": item.get("appearance_direction"),
            "style_tags": _clean_style_tags(item.get("style_tags") or []),
            "prompt_hint": item.get("prompt_hint"),
            "candidate_image_ids": [image.id],
            "note": "来自模特库，未试穿商品",
        },
    )
    db.add(candidate)
    await db.flush()
    model_settings.status = "approved"
    model_settings.approved_at = now
    model_settings.approved_by = user.id
    model_settings.output_json = {
        **(model_settings.output_json or {}),
        "style_prompt": style_prompt,
        "accessory_plan": accessory_plan,
        "selected_library_item_id": body.library_item_id,
        "selected_library_image_id": image.id,
    }
    candidate_step = await _step(db, run.id, "model_candidates")
    candidate_step.status = "needs_review"
    candidate_step.image_ids = _dedupe_nonempty(
        [*(candidate_step.image_ids or []), image.id]
    )
    candidate_step.input_json = {
        **(candidate_step.input_json or {}),
        "source": "model_library",
        "library_item_id": body.library_item_id,
        "style_prompt": style_prompt,
        "accessory_plan": accessory_plan,
    }
    candidate_step.output_json = {
        **(candidate_step.output_json or {}),
        "library_candidate_id": candidate.id,
        "library_candidate_image_id": image.id,
    }
    approval = await _step(db, run.id, "model_approval")
    if approval.status == "waiting_input":
        approval.status = "needs_review"
    approval.input_json = {
        **(approval.input_json or {}),
        "source": "model_library",
        "library_item_id": body.library_item_id,
        "style_prompt": style_prompt,
        "accessory_plan": accessory_plan,
    }
    run.current_step = "model_candidates"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates/{candidate_id}/save-to-library",
    response_model=ApparelModelLibraryItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def save_model_candidate_to_library(
    workflow_run_id: str,
    candidate_id: str,
    body: ModelCandidateSaveToLibraryIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> ApparelModelLibraryItemOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.id == candidate_id,
                ModelCandidate.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("not_found", "model candidate not found", 404)
    image_id = _primary_candidate_image_id(candidate)
    if not image_id:
        raise _http("candidate_image_missing", "candidate has no image to save", 422)
    item = await _add_user_library_item(
        db,
        user_id=user.id,
        source="favorite",
        image_id=image_id,
        title=body.title,
        age_segment=body.age_segment or _infer_age_segment_from_workflow(run),
        gender=body.gender,
        appearance_direction=body.appearance_direction,
        style_tags=body.style_tags,
    )
    brief = dict(candidate.model_brief_json or {})
    saved_ids = _dedupe_nonempty(
        [
            *(
                brief.get("saved_library_item_ids")
                if isinstance(brief.get("saved_library_item_ids"), list)
                else []
            ),
            str(item.get("id") or ""),
        ]
    )
    brief["saved_library_item_ids"] = saved_ids
    candidate.model_brief_json = brief
    await db.commit()
    # 项目流程里收藏到模特库：用户已经在标注里填了字段，但仍后台触发一次 vision
    # 校正/补全（appearance_direction / style_tags 默认空时常见）。
    item_id = str(item.get("id") or "")
    if item_id:
        background_tasks.add_task(_run_auto_tag_in_background, user.id, item_id)
    return _model_library_item_out(item)


@router.post(
    "/{workflow_run_id}/model-candidates/{candidate_id}/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_model_candidate(
    workflow_run_id: str,
    candidate_id: str,
    body: ModelCandidateApproveIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.id == candidate_id,
                ModelCandidate.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("not_found", "model candidate not found", 404)
    if candidate.status != "ready" or not candidate.contact_sheet_image_id:
        raise _http("candidate_not_ready", "model candidate is not ready to approve", 409)
    selected_accessory_image_id = body.selected_accessory_image_id
    approval = await _step(db, run.id, "model_approval")
    if selected_accessory_image_id:
        valid_accessory_image_id = (
            await db.execute(
                select(Image.id).where(
                    Image.id == selected_accessory_image_id,
                    Image.user_id == user.id,
                    Image.deleted_at.is_(None),
                    Image.id.in_(approval.image_ids or []),
                )
            )
        ).scalar_one_or_none()
        if valid_accessory_image_id is None:
            raise _http("invalid_accessory_image", "selected accessory preview is invalid", 400)
    all_candidates = (
        await db.execute(
            select(ModelCandidate).where(ModelCandidate.workflow_run_id == run.id)
        )
    ).scalars().all()
    now = _now()
    for row in all_candidates:
        if row.id == candidate.id:
            row.status = "selected"
            row.selected_at = now
            brief = dict(row.model_brief_json or {})
            brief["adjustments"] = body.adjustments
            brief["accessory_plan"] = body.accessory_plan.model_dump()
            brief["selected_accessory_image_id"] = selected_accessory_image_id
            row.model_brief_json = brief
        elif row.status != "failed":
            row.status = "rejected"
    approval.status = "approved"
    approval.approved_at = now
    approval.approved_by = user.id
    approval.input_json = {
        "candidate_id": candidate.id,
        "adjustments": body.adjustments,
        "accessory_plan": body.accessory_plan.model_dump(),
        "selected_accessory_image_id": selected_accessory_image_id,
    }
    approval.output_json = {
        "selected_candidate_id": candidate.id,
        "contact_sheet_image_id": candidate.contact_sheet_image_id,
        "selected_accessory_image_id": selected_accessory_image_id,
    }
    showcase = await _step(db, run.id, "showcase_generation")
    if showcase.status == "waiting_input":
        showcase.status = "needs_review"
    run.current_step = "model_approval"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates/reopen",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def reopen_model_selection(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidates = (
        await db.execute(
            select(ModelCandidate).where(ModelCandidate.workflow_run_id == run.id)
        )
    ).scalars().all()
    for candidate in candidates:
        if candidate.status in {"selected", "rejected"}:
            candidate.status = "ready" if candidate.contact_sheet_image_id else "generating"
            candidate.selected_at = None
    approval = await _step(db, run.id, "model_approval")
    previous_approval_input = dict(approval.input_json or {})
    candidate_step = await _step(db, run.id, "model_candidates")
    model_settings = await _step(db, run.id, "model_settings")
    product_step = await _step(db, run.id, "product_analysis")
    preserved_accessory_plan = (
        _coerce_accessory_plan_payload(previous_approval_input.get("accessory_plan"))
        or _coerce_accessory_plan_payload((candidate_step.input_json or {}).get("accessory_plan"))
        or _coerce_accessory_plan_payload((model_settings.output_json or {}).get("accessory_plan"))
        or _accessory_plan_from_product_analysis(product_step.output_json or {})
    )
    preserved_style_prompt = (
        str(previous_approval_input.get("style_prompt") or "").strip()
        or str((candidate_step.input_json or {}).get("style_prompt") or "").strip()
        or str((model_settings.output_json or {}).get("style_prompt") or "").strip()
    )
    if candidate_step.status != "running":
        candidate_step.status = "needs_review"
    approval.status = "needs_review"
    approval.approved_at = None
    approval.approved_by = None
    approval.input_json = {
        **({"accessory_plan": preserved_accessory_plan} if preserved_accessory_plan else {}),
        **({"style_prompt": preserved_style_prompt} if preserved_style_prompt else {}),
    }
    approval.output_json = {}
    approval.task_ids = []
    approval.image_ids = []
    showcase = await _step(db, run.id, "showcase_generation")
    showcase.status = "waiting_input"
    showcase.input_json = {}
    showcase.output_json = {}
    showcase.task_ids = []
    showcase.image_ids = []
    quality = await _step(db, run.id, "quality_review")
    quality.status = "waiting_input"
    quality.input_json = {}
    quality.output_json = {}
    quality.task_ids = []
    quality.image_ids = []
    await db.execute(delete(QualityReport).where(QualityReport.workflow_run_id == run.id))
    delivery = await _step(db, run.id, "delivery")
    delivery.status = "waiting_input"
    delivery.input_json = {}
    delivery.output_json = {}
    delivery.task_ids = []
    delivery.image_ids = []
    run.current_step = "model_candidates"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates/accessory-previews",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_accessory_previews(
    workflow_run_id: str,
    body: AccessoryPreviewCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.id == body.candidate_id,
                ModelCandidate.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("not_found", "model candidate not found", 404)
    if candidate.status != "selected" or not candidate.contact_sheet_image_id:
        raise _http(
            "model_not_selected",
            "select and approve a model candidate before generating accessory previews",
            409,
        )
    approval = await _step(db, run.id, "model_approval")
    conv = await _get_owned_conversation(db, user_id=user.id, conversation_id=run.conversation_id or "")
    brief = candidate.model_brief_json or {}
    age_context = " ".join(
        str(part)
        for part in (
            run.user_prompt,
            brief.get("summary") if isinstance(brief, dict) else None,
            body.style_prompt,
        )
        if part
    )
    bundle, _, gen_ids = await _create_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.IMAGE_TO_IMAGE,
        text=_accessory_preview_prompt(
            accessory_plan=body.accessory_plan.model_dump(),
            style_prompt=body.style_prompt,
            age_context=age_context,
        ),
        attachment_ids=[candidate.contact_sheet_image_id],
        idempotency_key=f"wf:{run.id[:12]}:acc:{candidate.id[:8]}:{new_uuid7()[:8]}",
        workflow_run_id=run.id,
        workflow_step_key="model_approval",
        image_params=_accessory_preview_image_params(),
        workflow_meta={
            "workflow_action": "accessory_preview",
            "workflow_candidate_id": candidate.id,
        },
    )
    approval.status = "running"
    approval.task_ids = _dedupe_nonempty([*(approval.task_ids or []), *gen_ids])
    approval.input_json = {
        **(approval.input_json or {}),
        "candidate_id": candidate.id,
        "accessory_plan": body.accessory_plan.model_dump(),
        "style_prompt": body.style_prompt,
    }
    run.current_step = "model_approval"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/model-candidates/accessory-selection",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def save_accessory_selection(
    workflow_run_id: str,
    body: AccessorySelectionIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    approval = await _step(db, run.id, "model_approval")
    selected_image_id = body.selected_accessory_image_id
    if selected_image_id:
        valid_image_id = (
            await db.execute(
                select(Image.id).where(
                    Image.id == selected_image_id,
                    Image.user_id == user.id,
                    Image.deleted_at.is_(None),
                    Image.id.in_(approval.image_ids or []),
                )
            )
        ).scalar_one_or_none()
        if valid_image_id is None:
            raise _http("invalid_accessory_image", "selected accessory preview is invalid", 400)
    approval.input_json = {
        **(approval.input_json or {}),
        "selected_accessory_image_id": selected_image_id,
    }
    approval.output_json = {
        **(approval.output_json or {}),
        "selected_accessory_image_id": selected_image_id,
    }
    run.current_step = "model_approval"
    if run.status not in {"running", "failed"}:
        run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def _selected_candidate(db: AsyncSession, run_id: str) -> ModelCandidate:
    candidate = (
        await db.execute(
            select(ModelCandidate).where(
                ModelCandidate.workflow_run_id == run_id,
                ModelCandidate.status == "selected",
            )
        )
    ).scalar_one_or_none()
    if candidate is None:
        raise _http("model_not_approved", "approve a model candidate first", 409)
    return candidate


@router.post(
    "/{workflow_run_id}/showcase-images",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_showcase_images(
    workflow_run_id: str,
    body: ShowcaseImagesCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    product_step = await _step(db, run.id, "product_analysis")
    if product_step.status != "approved":
        raise _http("product_not_approved", "approve product analysis first", 409)
    candidate = await _selected_candidate(db, run.id)
    if not candidate.contact_sheet_image_id:
        raise _http("missing_model_reference", "selected model has no reference image", 409)
    showcase = await _step(db, run.id, "showcase_generation")
    conv = await _get_owned_conversation(db, user_id=user.id, conversation_id=run.conversation_id or "")
    age_segment = _infer_age_segment_from_workflow(run)
    seed_key = f"{run.id}:{body.template}:{body.output_count}:{showcase.task_ids and len(showcase.task_ids) or 0}"
    shot_picks = _showcase_pick_shot_variants(
        template=body.template,
        age_segment=age_segment,
        output_count=body.output_count,
        seed_key=seed_key,
    )
    shot_plan = [shot_class for shot_class, _ in shot_picks]

    approval = await _step(db, run.id, "model_approval")
    accessory_plan = (approval.input_json or {}).get("accessory_plan")
    if not isinstance(accessory_plan, dict):
        accessory_plan = AccessoryPlanIn().model_dump()
    selected_accessory_image_id = (approval.input_json or {}).get("selected_accessory_image_id")
    ref_ids = _showcase_reference_image_ids(
        product_image_ids=run.product_image_ids,
        model_image_id=candidate.contact_sheet_image_id,
        selected_accessory_image_id=(
            selected_accessory_image_id if isinstance(selected_accessory_image_id, str) else None
        ),
    )
    existing_task_ids = _dedupe_nonempty(showcase.task_ids or [])
    existing_image_ids = _dedupe_nonempty(showcase.image_ids or [])
    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    for idx, (shot_type, variant) in enumerate(shot_picks, start=1):
        bundle, _, gen_ids = await _create_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=Intent.IMAGE_TO_IMAGE,
            text=_showcase_prompt(
                product_analysis=product_step.output_json or {},
                selected_candidate=candidate,
                accessory_plan=accessory_plan,
                template=body.template,
                shot_type=shot_type,
                shot_variant=variant,
                age_segment=age_segment,
                final_quality=body.final_quality,
                user_prompt=run.user_prompt,
                aspect_ratio=body.aspect_ratio,
                scene_environment=body.scene_environment,
            ),
            attachment_ids=ref_ids,
            idempotency_key=f"wf:{run.id[:12]}:shot:{idx}:{new_uuid7()[:8]}",
            workflow_run_id=run.id,
            workflow_step_key="showcase_generation",
            image_params=_image_params(
                aspect_ratio=body.aspect_ratio,
                count=1,
                render_quality="high" if body.final_quality != "standard" else "medium",
                final_quality=body.final_quality,
                fast=False,
            ),
            workflow_meta={
                "workflow_action": "showcase_image",
                "workflow_candidate_id": candidate.id,
                "workflow_shot_type": shot_type,
                "workflow_shot_variant": variant["label"],
                "workflow_shot_framing": variant["framing"],
                "workflow_template": body.template,
                "workflow_age_segment": age_segment,
                "workflow_final_quality": body.final_quality,
                "workflow_scene_environment": body.scene_environment,
            },
        )
        task_ids.extend(gen_ids)
        bundles.append(bundle)
    showcase.status = "running"
    showcase.task_ids = _dedupe_nonempty([*existing_task_ids, *task_ids])
    showcase.image_ids = existing_image_ids
    showcase.input_json = {
        "template": body.template,
        "shot_plan": shot_plan,
        "shot_variants": [
            {"shot_class": cls, "label": v["label"], "framing": v["framing"]}
            for cls, v in shot_picks
        ],
        "age_segment": age_segment,
        "aspect_ratio": body.aspect_ratio,
        "final_quality": body.final_quality,
        "output_count": body.output_count,
        "scene_environment": body.scene_environment,
        "target_image_count": _showcase_target_image_count(
            existing_image_ids=existing_image_ids,
            output_count=body.output_count,
        ),
        "reference_image_ids": ref_ids,
    }
    quality = await _step(db, run.id, "quality_review")
    quality.status = "waiting_input"
    quality.input_json = {}
    quality.output_json = {}
    quality.task_ids = []
    quality.image_ids = []
    run.current_step = "showcase_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/images/{image_id}/revise",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def revise_showcase_image(
    workflow_run_id: str,
    image_id: str,
    body: ImageRevisionIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    showcase = await _step(db, run.id, "showcase_generation")
    if image_id not in set(showcase.image_ids or []):
        raise _http("invalid_image", "image is not a showcase output for this workflow", 404)
    image = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user.id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if image is None:
        raise _http("not_found", "image not found", 404)
    product_step = await _step(db, run.id, "product_analysis")
    candidate = await _selected_candidate(db, run.id)
    refs = _dedupe_nonempty([*run.product_image_ids, candidate.contact_sheet_image_id or "", image_id])
    conv = await _get_owned_conversation(db, user_id=user.id, conversation_id=run.conversation_id or "")
    revision_index = len(showcase.task_ids or []) + 1
    bundle, _, gen_ids = await _create_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.IMAGE_TO_IMAGE,
        text=_revision_prompt(
            instruction=body.instruction,
            product_analysis=product_step.output_json or {},
            selected_candidate=candidate,
        ),
        attachment_ids=refs,
        idempotency_key=f"wf:{run.id[:22]}:rev:{revision_index}",
        workflow_run_id=run.id,
        workflow_step_key="showcase_generation",
        image_params=_image_params(aspect_ratio="4:5", count=1, render_quality="high"),
        workflow_meta={
            "workflow_action": "revision",
            "workflow_revision_source_image_id": image_id,
            "workflow_revision_scope": body.scope,
        },
    )
    showcase.task_ids = [*(showcase.task_ids or []), *gen_ids]
    showcase.status = "running"
    quality = await _step(db, run.id, "quality_review")
    quality.status = "waiting_input"
    quality.input_json = {
        **(quality.input_json or {}),
        "latest_revision": {
            "source_image_id": image_id,
            "instruction": body.instruction,
            "scope": body.scope,
        },
    }
    run.current_step = "showcase_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/delivery/complete",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def complete_delivery(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    await _sync_workflow_outputs(db, run)
    showcase = await _step(db, run.id, "showcase_generation")
    if not showcase.image_ids:
        raise _http("no_outputs", "generate showcase images before delivery", 409)
    quality = await _step(db, run.id, "quality_review")
    delivery = await _step(db, run.id, "delivery")
    now = _now()
    quality.status = "approved"
    quality.approved_at = now
    quality.approved_by = user.id
    delivery.status = "completed"
    delivery.approved_at = now
    delivery.approved_by = user.id
    delivery.input_json = {"final_image_ids": showcase.image_ids}
    delivery.output_json = {
        "download_image_ids": showcase.image_ids,
        "completed_at": now.isoformat(),
    }
    run.status = "completed"
    run.current_step = "delivery"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


# ---------------------------------------------------------------------------
# 模特库独立生成 + 任务中心聚合 + vision 自动打标签
#
# 设计要点：
# 1. 每次"生成 N 张模特"请求 = 一条隐藏的 WorkflowRun(type=
#    apparel_model_library_generate) + 1 个 step(step_key=
#    model_library_generate) + N 个 worker generation task。每张产出一张独立
#    模特肖像；不创建 ModelCandidate（不和项目里的"候选 4 视图"逻辑混淆）。
# 2. 任务中心同时聚合两类来源：模特库独立生成 + 项目里的 model_candidates
#    step（origin 字段区分）。
# 3. 自动打标签走 worker tasks/model_library_tagging.py 调 vision provider；
#    解析失败 graceful，不影响主流程。
# ---------------------------------------------------------------------------


_MODEL_LIBRARY_TITLE_AGE_LABELS: dict[str, str] = {
    "user_favorites": "收藏",
    "toddler": "幼儿",
    "child": "儿童",
    "teen": "青少年",
    "young_adult": "青年",
    "adult": "熟龄",
    "middle_aged": "中年",
    "senior": "老年",
}


def _model_library_generate_genders(body: ApparelModelLibraryGenerateIn) -> list[str]:
    raw = getattr(body, "genders", None)
    genders = _dedupe_nonempty(raw or [])
    if not genders and body.gender:
        genders = [body.gender]
    genders = [gender for gender in genders if gender in {"female", "male"}]
    return genders or ["female"]


def _model_library_gender_label(genders: list[str]) -> str:
    if set(genders) == {"female", "male"}:
        return "男女"
    if not genders:
        return "女性"
    return "女性" if genders[0] == "female" else "男性"


def _model_library_run_title(
    *,
    age_segment: str,
    gender: str | None = None,
    genders: list[str] | None = None,
    appearance_direction: str | None,
) -> str:
    age_label = _MODEL_LIBRARY_TITLE_AGE_LABELS.get(age_segment, age_segment)
    gender_label = _model_library_gender_label(genders or ([gender] if gender else []))
    appearance = (appearance_direction or "").strip()
    parts = [f"{age_label}{gender_label}"]
    if appearance:
        parts.append(appearance[:24])
    title = " · ".join(["模特库生成", *parts])
    return title[:120]


def _model_library_generate_prompt(
    *,
    age_segment: str,
    gender: str,
    appearance_direction: str | None,
    extra_requirements: str | None,
    style_tags: list[str],
    candidate_index: int,
) -> str:
    """构造一张 2x2 contact sheet 模特参考图的 prompt。

    与项目流程 _candidate_prompt 同样格式（4 视图：正面 / 左侧面 / 背面 / 大头照），
    这样库里的条目可以直接被 select_apparel_model_library_item 顶替项目候选使用。
    每次按 count 并行生成 N 张独立模特的 contact sheet，candidate_index 用来引导差异化。
    """
    gender_label = "female" if gender == "female" else "male"
    appearance = (appearance_direction or "").strip()
    extras = (extra_requirements or "").strip()
    tag_text = ", ".join(_clean_style_tags(style_tags)) if style_tags else ""
    age_directive = ""
    if age_segment == "toddler":
        age_directive = "age 2-4, toddler proportions"
    elif age_segment == "child":
        age_directive = "age 5-12, child proportions"
    elif age_segment == "teen":
        age_directive = "age 13-17, teen proportions"
    elif age_segment == "young_adult":
        age_directive = "age 18-29, young adult proportions"
    elif age_segment == "adult":
        age_directive = "age 30-44, mature adult proportions"
    elif age_segment == "middle_aged":
        age_directive = "age 45-59, middle-aged adult proportions"
    elif age_segment == "senior":
        age_directive = "age 60 or older, senior adult proportions"
    base_styling = "warm ivory sleeveless top and warm ivory shorts, barefoot"
    appearance_directive = (
        f"Appearance direction: {appearance}." if appearance else ""
    )
    style_directive = f"Style references: {tag_text}." if tag_text else ""
    extras_directive = f"User notes: {extras}." if extras else ""
    diversity = _model_diversity_anchor(
        candidate_index=candidate_index,
        gender=gender,
        age_segment=age_segment,
    )
    return " ".join(
        part
        for part in [
            "Create one clean 2x2 ecommerce model reference contact sheet, exactly four panels: "
            "top-left front full body, top-right left 90-degree profile full body, "
            "bottom-left straight back full body, bottom-right close-up headshot.",
            "Same model in all four panels, consistent framing, "
            "same camera height and distance for the three full-body views.",
            "Side panel must be a true left profile (only one eye visible, "
            "body fully sideways, not a three-quarter pose).",
            "Back panel must hide the face. Headshot must be straight frontal with both eyes visible.",
            "Plain seamless white or light gray studio background, soft even lighting, "
            "no props, no text labels.",
            "Real commercially photographed person, not an AI beauty render.",
            f"Use simple neutral base clothing: {base_styling}.",
            "Every candidate must wear this exact same outfit; "
            "only face, hair, and body type may differ between candidates.",
            f"Gender: {gender_label}. {age_directive}".strip(),
            appearance_directive,
            style_directive,
            extras_directive,
            diversity,
            f"Variation index: {candidate_index}.",
        ]
        if part
    ).strip()


def _model_library_generate_image_params() -> ImageParamsIn:
    """模特库独立生成 2x2 contact sheet：4:5 跟项目候选一致，PNG 高质量。"""
    params = _image_params(
        aspect_ratio="4:5",
        count=1,
        render_quality="high",
        fast=False,
    )
    return params.model_copy(update={"output_format": "png", "output_compression": None})


def _model_library_run_inputs(step: WorkflowStep) -> dict[str, Any]:
    """从 step.input_json 拿生成请求快照（age_segment / gender 等）。"""
    raw = step.input_json if isinstance(step.input_json, dict) else {}
    genders = _dedupe_nonempty(raw.get("genders") or [])
    genders = [gender for gender in genders if gender in {"female", "male"}]
    gender = (
        "/".join(genders)
        if len(genders) > 1
        else _normalize_model_gender(genders[0] if genders else raw.get("gender"))
    )
    return {
        "age_segment": _normalize_age_segment(raw.get("age_segment")),
        "gender": gender,
        "genders": genders,
        "appearance_direction": _clean_optional_text(
            raw.get("appearance_direction"), max_len=80
        ),
        "extra_requirements": _clean_optional_text(
            raw.get("extra_requirements"), max_len=400
        ),
        "style_tags": _clean_style_tags(raw.get("style_tags") or []),
        "auto_tag": bool(raw.get("auto_tag", True)),
        "count": int(raw.get("count") or 0) or len(step.task_ids or []),
    }


async def _saved_image_id_set(
    db: AsyncSession, user_id: str
) -> dict[str, str]:
    """{ image_id -> library_item_id } map: 看哪些图已经收藏到当前用户的库。"""
    rows = (
        await db.execute(
            select(ModelLibraryItem.image_id, ModelLibraryItem.id)
            .where(ModelLibraryItem.user_id == user_id)
            .order_by(ModelLibraryItem.created_at.asc())
        )
    ).all()
    out: dict[str, str] = {}
    for image_id, item_id in rows:
        if not image_id or not item_id:
            continue
        out.setdefault(str(image_id), str(item_id))
    return out


def _model_library_job_status(
    *,
    step_status: str,
    requested_count: int,
    finished_count: int,
) -> str:
    """从 step.status + 张数推导 job 聚合 status。"""
    if step_status == "failed":
        return "partial" if finished_count > 0 else "failed"
    if step_status in {"approved", "completed", "needs_review", "succeeded"}:
        if requested_count > 0 and finished_count >= requested_count:
            return "succeeded"
        if finished_count > 0:
            return "partial"
        return "succeeded" if step_status == "succeeded" else "failed"
    if step_status == "running":
        return "running"
    return "queued"


async def _gather_job_image_outs(
    db: AsyncSession,
    *,
    user_id: str,
    image_ids: list[str],
) -> dict[str, ImageOut]:
    if not image_ids:
        return {}
    images = list(
        (
            await db.execute(
                select(Image)
                .where(
                    Image.id.in_(image_ids),
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    )
    return await _image_out_map(db, images)


async def _model_library_image_meta_by_id(
    db: AsyncSession,
    *,
    user_id: str,
    image_ids: list[str],
) -> dict[str, dict[str, Any]]:
    ids = _dedupe_nonempty(image_ids)
    if not ids:
        return {}
    images = list(
        (
            await db.execute(
                select(Image)
                .where(
                    Image.id.in_(ids),
                    Image.user_id == user_id,
                    Image.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    )
    gen_ids = _dedupe_nonempty(
        image.owner_generation_id or "" for image in images
    )
    generation_req: dict[str, dict[str, Any]] = {}
    if gen_ids:
        generations = list(
            (
                await db.execute(
                    select(Generation)
                    .where(
                        Generation.id.in_(gen_ids),
                        Generation.user_id == user_id,
                    )
                )
            ).scalars().all()
        )
        generation_req = {
            generation.id: dict(generation.upstream_request or {})
            for generation in generations
            if isinstance(generation.upstream_request, dict)
        }

    out: dict[str, dict[str, Any]] = {}
    for image in images:
        meta: dict[str, Any] = {"mime": image.mime}
        stored = image.metadata_jsonb if isinstance(image.metadata_jsonb, dict) else {}
        parsed = parse_model_image_metadata(stored.get("model_library"))
        if parsed is not None:
            meta.update(
                {
                    "age_segment": parsed.age_segment,
                    "gender": parsed.gender,
                    "appearance_direction": parsed.appearance_direction,
                    "style_tags": list(parsed.style_tags or []),
                    "prompt_hint": parsed.prompt_hint,
                }
            )
        filename = _clean_optional_text(stored.get("suggested_filename"), max_len=160)
        if filename:
            meta["download_filename"] = filename
        for key in (
            "is_dual_race_bonus",
            "billing_free",
            "billing_label",
            "billing_exempt_reason",
        ):
            if key in stored:
                meta[key] = stored[key]

        req = generation_req.get(image.owner_generation_id or "", {})
        if req:
            for key in (
                "is_dual_race_bonus",
                "billing_free",
                "billing_label",
                "billing_exempt_reason",
            ):
                if key in req and key not in meta:
                    meta[key] = req[key]
            if not meta.get("age_segment"):
                meta["age_segment"] = _clean_optional_text(
                    req.get("workflow_model_library_age_segment"), max_len=32
                )
            if not meta.get("gender"):
                meta["gender"] = _clean_optional_text(
                    req.get("workflow_model_library_gender"), max_len=16
                )
            if not meta.get("appearance_direction"):
                meta["appearance_direction"] = _clean_optional_text(
                    req.get("workflow_model_library_appearance_direction"),
                    max_len=80,
                )
            if not meta.get("style_tags"):
                meta["style_tags"] = _clean_style_tags(
                    req.get("workflow_model_library_style_tags") or []
                )
        out[image.id] = meta
    return out


def _job_item_out(
    *,
    image_id: str,
    image_out: ImageOut | None,
    saved_item_id: str | None,
    age_segment: str | None,
    gender: str | None,
    style_tags: list[str],
    appearance_direction: str | None,
    image_meta: dict[str, Any] | None = None,
) -> ApparelModelLibraryJobItemOut:
    if image_out is not None:
        image_url = image_out.url
        display_url = image_out.display_url
        thumb_url = image_out.thumb_url
    else:
        image_url = _image_url(image_id)
        display_url = None
        thumb_url = None
    meta = image_meta or {}
    resolved_tags = _clean_style_tags(
        [*(meta.get("style_tags") or []), *style_tags]
    )
    resolved_age = _normalize_age_segment(meta.get("age_segment") or age_segment)
    if resolved_age == "user_favorites" and age_segment:
        resolved_age = _normalize_age_segment(age_segment)
    resolved_gender = _clean_optional_text(
        meta.get("gender") or gender, max_len=40
    )
    resolved_appearance = _clean_optional_text(
        meta.get("appearance_direction") or appearance_direction,
        max_len=80,
    )
    filename = _clean_optional_text(meta.get("download_filename"), max_len=160)
    if not filename:
        filename = _model_library_download_filename(
            image_id=image_id,
            mime=(image_out.mime if image_out is not None else meta.get("mime")),
            age_segment=resolved_age,
            gender=resolved_gender,
            appearance_direction=resolved_appearance,
            style_tags=resolved_tags,
        )
    is_dual_race_bonus = bool(
        meta.get("is_dual_race_bonus")
        or (
            getattr(image_out, "is_dual_race_bonus", False)
            if image_out is not None
            else False
        )
    )
    billing_label = _clean_optional_text(
        meta.get("billing_label")
        or (
            getattr(image_out, "billing_label", None)
            if image_out is not None
            else None
        ),
        max_len=32,
    )
    billing_free = bool(
        meta.get("billing_free")
        or (
            getattr(image_out, "billing_free", False)
            if image_out is not None
            else False
        )
        or is_dual_race_bonus
        or billing_label == "free"
    )
    if billing_free and not billing_label:
        billing_label = "free"
    billing_exempt_reason = _clean_optional_text(
        meta.get("billing_exempt_reason")
        or (
            getattr(image_out, "billing_exempt_reason", None)
            if image_out is not None
            else None
        ),
        max_len=80,
    )
    return ApparelModelLibraryJobItemOut(
        image_id=image_id,
        image_url=image_url,
        display_url=display_url,
        thumb_url=thumb_url,
        saved_item_id=saved_item_id,
        style_tags=resolved_tags,
        appearance_direction=resolved_appearance,
        gender=resolved_gender,
        download_filename=filename,
        is_dual_race_bonus=is_dual_race_bonus,
        billing_free=billing_free,
        billing_label=billing_label,
        billing_exempt_reason=billing_exempt_reason,
    )


def _extract_bonus_ids(
    step: WorkflowStep | None, image_ids: Iterable[str]
) -> list[str]:
    """从 step.output_json 提取 dual_race_bonus 图片 ids，去除已在 image_ids 里的重叠"""
    if step is None:
        return []
    output = step.output_json or {}
    raw = output.get("dual_race_bonus_image_ids") or []
    if not isinstance(raw, list):
        return []
    seen = set(image_ids)
    return [bid for bid in raw if isinstance(bid, str) and bid not in seen]


async def _workflow_produced_model_image_ids(
    db: AsyncSession,
    *,
    user_id: str,
    steps: list[WorkflowStep],
) -> set[str]:
    """Image ids produced by a model workflow, including dual_race bonus outputs."""
    produced = {
        iid
        for step in steps
        for iid in (step.image_ids or [])
        if isinstance(iid, str) and iid
    }
    for step in steps:
        produced.update(_extract_bonus_ids(step, produced))

    all_task_ids = _dedupe_nonempty(
        task_id for step in steps for task_id in (step.task_ids or [])
    )
    if not all_task_ids:
        return produced

    owned = (
        await db.execute(
            select(Image.id).where(
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
                or_(
                    Image.owner_generation_id.in_(all_task_ids),
                    Image.owner_generation_id.in_(
                        select(Generation.id).where(
                            Generation.user_id == user_id,
                            Generation.upstream_request[
                                "parent_generation_id"
                            ].astext.in_(all_task_ids),
                            Generation.upstream_request[
                                "is_dual_race_bonus"
                            ].as_boolean().is_(True),
                        )
                    ),
                ),
            )
        )
    ).scalars().all()
    produced.update(iid for iid in owned if isinstance(iid, str) and iid)
    return produced


async def _job_from_library_run(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    saved_map: dict[str, str],
) -> ApparelModelLibraryJobOut:
    step = (
        await db.execute(
            select(WorkflowStep).where(
                WorkflowStep.workflow_run_id == run.id,
                WorkflowStep.step_key == MODEL_LIBRARY_GENERATE_STEP_KEY,
            )
        )
    ).scalar_one_or_none()
    inputs: dict[str, Any] = {}
    image_ids: list[str] = []
    requested = 0
    step_status = "queued"
    if step is not None:
        inputs = _model_library_run_inputs(step)
        image_ids = [iid for iid in (step.image_ids or []) if isinstance(iid, str)]
        requested = max(
            inputs.get("count") or 0,
            len(step.task_ids or []),
            len(image_ids),
        )
        step_status = step.status
    finished = len(image_ids)
    # dual_race loser 写回的 bonus image_ids（与 winner image_ids 物理隔离）
    bonus_ids = _extract_bonus_ids(step, image_ids)
    # 一次查询拿到 winner + bonus 全部 image meta，省一次 DB roundtrip
    image_out_map = await _gather_job_image_outs(
        db, user_id=run.user_id, image_ids=image_ids + bonus_ids
    )
    image_meta_map = await _model_library_image_meta_by_id(
        db, user_id=run.user_id, image_ids=image_ids + bonus_ids
    )
    tagging_results = (step.output_json or {}).get("tagging_results") if step else None
    tagging_map: dict[str, dict[str, Any]] = (
        tagging_results if isinstance(tagging_results, dict) else {}
    )
    items = [
        _job_item_out(
            image_id=iid,
            image_out=image_out_map.get(iid),
            saved_item_id=saved_map.get(iid),
            age_segment=inputs.get("age_segment"),
            gender=(image_meta_map.get(iid) or {}).get("gender")
            or (tagging_map.get(iid) or {}).get("gender")
            or inputs.get("gender"),
            style_tags=_clean_style_tags(
                [
                    *(inputs.get("style_tags") or []),
                    *((tagging_map.get(iid) or {}).get("style_tags") or []),
                ]
            ),
            appearance_direction=(tagging_map.get(iid) or {}).get(
                "appearance_direction"
            ),
            image_meta=image_meta_map.get(iid),
        )
        for iid in image_ids
    ]
    # candidate（loser）不跑 tagging，但可沿用任务元信息手动入库。
    candidates = [
        _job_item_out(
            image_id=bid,
            image_out=image_out_map.get(bid),
            saved_item_id=saved_map.get(bid),
            age_segment=inputs.get("age_segment"),
            gender=(image_meta_map.get(bid) or {}).get("gender") or inputs.get("gender"),
            style_tags=inputs.get("style_tags") or [],
            appearance_direction=inputs.get("appearance_direction"),
            image_meta=image_meta_map.get(bid),
        )
        for bid in bonus_ids
    ]
    error_message = None
    if step is not None:
        out_json = step.output_json if isinstance(step.output_json, dict) else {}
        error_message = _clean_optional_text(out_json.get("error_message"), max_len=400)
        task_generations = await _workflow_generation_rows_from_task_ids(
            db,
            user_id=run.user_id,
            task_ids=list(step.task_ids or []),
            include_dual_bonus=False,
        )
        failed_generations = [
            generation
            for generation in task_generations
            if generation.status == GenerationStatus.FAILED.value
        ]
        active_generations = [
            generation
            for generation in task_generations
            if generation.status
            in {GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value}
        ]
        if failed_generations and not active_generations and finished < requested:
            if step_status == "running":
                step_status = "failed"
            if error_message is None:
                error_message = _clean_optional_text(
                    _task_error_summary(failed_generations, "模特库生成失败"),
                    max_len=400,
                )
    job_status = _model_library_job_status(
        step_status=step_status,
        requested_count=requested,
        finished_count=finished,
    )
    return ApparelModelLibraryJobOut(
        job_id=run.id,
        origin="library_generate",
        workflow_run_id=run.id,
        project_title=None,
        status=job_status,  # type: ignore[arg-type]
        requested_count=requested,
        finished_count=finished,
        age_segment=inputs.get("age_segment"),
        gender=inputs.get("gender"),
        appearance_direction=inputs.get("appearance_direction"),
        extra_requirements=inputs.get("extra_requirements"),
        items=items,
        candidates=candidates,
        error_message=error_message,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


async def _job_from_project_candidate_step(
    db: AsyncSession,
    *,
    run: WorkflowRun,
    step: WorkflowStep,
    saved_map: dict[str, str],
) -> ApparelModelLibraryJobOut:
    image_ids = [iid for iid in (step.image_ids or []) if isinstance(iid, str)]
    requested_count = MODEL_CANDIDATE_COUNT
    raw_input = step.input_json if isinstance(step.input_json, dict) else {}
    candidate_count = raw_input.get("candidate_count")
    if isinstance(candidate_count, int) and candidate_count > 0:
        requested_count = candidate_count
    # dual_race loser 写回的 bonus image_ids（如该 origin 也走 dual_race）
    bonus_ids = _extract_bonus_ids(step, image_ids)
    image_out_map = await _gather_job_image_outs(
        db, user_id=run.user_id, image_ids=image_ids + bonus_ids
    )
    image_meta_map = await _model_library_image_meta_by_id(
        db, user_id=run.user_id, image_ids=image_ids + bonus_ids
    )
    profile = (run.metadata_jsonb or {}).get("model_profile") or {}
    age_segment = (
        _normalize_age_segment(profile.get("age_segment"))
        if isinstance(profile, dict)
        else None
    )
    gender = profile.get("gender") if isinstance(profile, dict) else None
    appearance_direction = (
        profile.get("appearance_direction")
        if isinstance(profile, dict)
        else None
    )
    items = [
        _job_item_out(
            image_id=iid,
            image_out=image_out_map.get(iid),
            saved_item_id=saved_map.get(iid),
            age_segment=age_segment,
            gender=gender,
            style_tags=[],
            appearance_direction=appearance_direction,
            image_meta=image_meta_map.get(iid),
        )
        for iid in image_ids
    ]
    candidates = [
        _job_item_out(
            image_id=bid,
            image_out=image_out_map.get(bid),
            saved_item_id=saved_map.get(bid),
            age_segment=age_segment,
            gender=gender,
            style_tags=[],
            appearance_direction=appearance_direction,
            image_meta=image_meta_map.get(bid),
        )
        for bid in bonus_ids
    ]
    out_json = step.output_json if isinstance(step.output_json, dict) else {}
    error_message = _clean_optional_text(out_json.get("error_message"), max_len=400)
    step_status = step.status
    task_generations = await _workflow_generation_rows_from_task_ids(
        db,
        user_id=run.user_id,
        task_ids=list(step.task_ids or []),
        include_dual_bonus=False,
    )
    failed_generations = [
        generation
        for generation in task_generations
        if generation.status == GenerationStatus.FAILED.value
    ]
    active_generations = [
        generation
        for generation in task_generations
        if generation.status
        in {GenerationStatus.QUEUED.value, GenerationStatus.RUNNING.value}
    ]
    if failed_generations and not active_generations and len(image_ids) < requested_count:
        if step_status == "running":
            step_status = "failed"
        if error_message is None:
            error_message = _clean_optional_text(
                _task_error_summary(failed_generations, "项目模特候选生成失败"),
                max_len=400,
            )
    job_status = _model_library_job_status(
        step_status=step_status,
        requested_count=requested_count,
        finished_count=len(image_ids),
    )
    return ApparelModelLibraryJobOut(
        job_id=f"{run.id}:model_candidates",
        origin="project_candidate",
        workflow_run_id=run.id,
        project_title=run.title,
        status=job_status,  # type: ignore[arg-type]
        requested_count=requested_count,
        finished_count=len(image_ids),
        age_segment=age_segment,
        gender=gender,
        appearance_direction=appearance_direction,
        extra_requirements=None,
        items=items,
        candidates=candidates,
        error_message=error_message,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


async def _enqueue_model_library_generate_tasks(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    run: WorkflowRun,
    step: WorkflowStep,
    body: ApparelModelLibraryGenerateIn,
) -> tuple[list[_PublishBundle], list[str]]:
    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    genders = _model_library_generate_genders(body)
    task_index = 0
    for gender in genders:
        for idx in range(1, int(body.count) + 1):
            task_index += 1
            prompt = _model_library_generate_prompt(
                age_segment=body.age_segment,
                gender=gender,
                appearance_direction=body.appearance_direction,
                extra_requirements=body.extra_requirements,
                style_tags=body.style_tags,
                candidate_index=idx,
            )
            bundle, _, gen_ids = await _create_workflow_task(
                db=db,
                user=user,
                conv=conv,
                intent=Intent.TEXT_TO_IMAGE,
                text=prompt,
                attachment_ids=[],
                idempotency_key=f"mlib:{run.id[:24]}:{gender}:{idx}",
                workflow_run_id=run.id,
                workflow_step_key=MODEL_LIBRARY_GENERATE_STEP_KEY,
                image_params=_model_library_generate_image_params(),
                workflow_meta={
                    "workflow_action": MODEL_LIBRARY_GENERATE_WORKER_ACTION,
                    "workflow_candidate_index": task_index,
                    "workflow_model_library_age_segment": body.age_segment,
                    "workflow_model_library_gender": gender,
                    "workflow_model_library_appearance_direction": (
                        body.appearance_direction or ""
                    ),
                    "workflow_model_library_style_tags": _clean_style_tags(
                        body.style_tags
                    ),
                    "workflow_model_library_auto_tag": bool(body.auto_tag),
                },
            )
            task_ids.extend(gen_ids)
            bundles.append(bundle)
    step.task_ids = task_ids
    return bundles, task_ids


@router.post(
    "/apparel-model-library/generate",
    response_model=ApparelModelLibraryJobOut,
    dependencies=[Depends(verify_csrf)],
)
async def generate_apparel_model_library_job(
    body: ApparelModelLibraryGenerateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelModelLibraryJobOut:
    """模特库独立生成入口。

    创建一条隐藏 WorkflowRun + 一个 step + N 个 worker generation task。
    返回一个 Job 视图（status=queued/running，items=空，前端再轮询 GET /jobs）。
    """
    if int(body.count) not in MODEL_LIBRARY_GENERATE_COUNTS:
        raise _http(
            "invalid_count",
            f"count must be one of {sorted(MODEL_LIBRARY_GENERATE_COUNTS)}",
            422,
        )
    genders = _model_library_generate_genders(body)
    title = _model_library_run_title(
        age_segment=body.age_segment,
        gender=body.gender,
        genders=genders,
        appearance_direction=body.appearance_direction,
    )
    conv = await _get_or_create_workflow_conversation(
        db,
        user=user,
        conversation_id=None,
        title=title,
        workflow_type=WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
    )
    conv.title = title
    conv.archived = True
    run = WorkflowRun(
        conversation_id=conv.id,
        user_id=user.id,
        type=WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
        status="running",
        title=title,
        user_prompt=body.extra_requirements or "",
        product_image_ids=[],
        current_step=MODEL_LIBRARY_GENERATE_STEP_KEY,
        quality_mode="standard",
        metadata_jsonb={
            "template": "apparel_model_library_generate",
            "model_profile": {
                "age_segment": body.age_segment,
                "gender": genders[0],
                "genders": genders,
                "appearance_direction": body.appearance_direction,
            },
        },
    )
    db.add(run)
    await db.flush()
    step = WorkflowStep(
        workflow_run_id=run.id,
        step_key=MODEL_LIBRARY_GENERATE_STEP_KEY,
        status="running",
        input_json={
            "age_segment": body.age_segment,
            "gender": genders[0],
            "genders": genders,
            "appearance_direction": body.appearance_direction,
            "extra_requirements": body.extra_requirements,
            "style_tags": _clean_style_tags(body.style_tags),
            "count": int(body.count),
            "count_per_gender": int(body.count),
            "auto_tag": bool(body.auto_tag),
        },
        output_json={},
    )
    db.add(step)
    await db.flush()
    bundles, _ = await _enqueue_model_library_generate_tasks(
        db=db,
        user=user,
        conv=conv,
        run=run,
        step=step,
        body=body,
    )
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    await _ensure_legacy_user_library_migrated(db, user.id)
    saved_map = await _saved_image_id_set(db, user.id)
    run = await _get_run(db, user_id=user.id, run_id=run.id)
    job = await _job_from_library_run(db, run=run, saved_map=saved_map)
    await db.commit()
    return job


@router.get(
    "/apparel-model-library/jobs",
    response_model=ApparelModelLibraryJobsOut,
)
async def list_apparel_model_library_jobs(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ApparelModelLibraryJobsOut:
    """聚合任务中心：模特库独立生成 + 项目候选 step。"""
    migrated_legacy = await _ensure_legacy_user_library_migrated(db, user.id)
    saved_map = await _saved_image_id_set(db, user.id)
    fetch_limit = offset + limit + 1
    library_runs = list(
        (
            await db.execute(
                select(WorkflowRun)
                .where(
                    WorkflowRun.user_id == user.id,
                    WorkflowRun.deleted_at.is_(None),
                    WorkflowRun.type == WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
                )
                .order_by(desc(WorkflowRun.updated_at), desc(WorkflowRun.id))
                .limit(fetch_limit)
            )
        ).scalars().all()
    )
    library_jobs: list[ApparelModelLibraryJobOut] = []
    for run in library_runs:
        library_jobs.append(
            await _job_from_library_run(db, run=run, saved_map=saved_map)
        )

    candidate_rows = list(
        (
            await db.execute(
                select(WorkflowRun, WorkflowStep)
                .join(WorkflowStep, WorkflowStep.workflow_run_id == WorkflowRun.id)
                .where(
                    WorkflowRun.user_id == user.id,
                    WorkflowRun.deleted_at.is_(None),
                    WorkflowRun.type == WORKFLOW_TYPE,
                    WorkflowStep.step_key == "model_candidates",
                    WorkflowStep.status.in_(
                        [
                            "queued",
                            "running",
                            "succeeded",
                            "failed",
                            "needs_review",
                            "approved",
                            "completed",
                        ]
                    ),
                )
                .order_by(desc(WorkflowRun.updated_at), desc(WorkflowRun.id))
                .limit(fetch_limit)
            )
        ).all()
    )
    project_jobs: list[ApparelModelLibraryJobOut] = []
    for run_obj, step in candidate_rows:
        project_jobs.append(
            await _job_from_project_candidate_step(
                db, run=run_obj, step=step, saved_map=saved_map
            )
        )

    merged = sorted(
        [*library_jobs, *project_jobs],
        key=lambda job: job.updated_at or job.created_at,
        reverse=True,
    )
    page = merged[offset : offset + limit]
    if migrated_legacy:
        await db.commit()
    return ApparelModelLibraryJobsOut(
        items=page,
        limit=limit,
        offset=offset,
        has_more=len(merged) > offset + limit,
    )


@router.delete(
    "/apparel-model-library/jobs/{workflow_run_id}",
    dependencies=[Depends(verify_csrf)],
)
async def delete_apparel_model_library_job(
    workflow_run_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, bool]:
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE:
        raise _http(
            "invalid_workflow_type",
            "only standalone model-library jobs can be cleaned here",
            400,
        )
    deleted_at = _now()
    await _soft_delete_workflow_generated_images(
        db,
        run=run,
        deleted_at=deleted_at,
        cancel_message="model library job deleted",
    )
    run.deleted_at = deleted_at
    if run.conversation_id:
        conv = (
            await db.execute(
                select(Conversation).where(
                    Conversation.id == run.conversation_id,
                    Conversation.user_id == user.id,
                    Conversation.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if conv is not None:
            conv.deleted_at = deleted_at
    await db.commit()
    return {"ok": True}


@router.delete(
    "/apparel-model-library/jobs",
    response_model=ApparelModelLibraryJobsClearOut,
    dependencies=[Depends(verify_csrf)],
)
async def clear_apparel_model_library_jobs(
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelModelLibraryJobsClearOut:
    rows = list(
        (
            await db.execute(
                select(WorkflowRun).where(
                    WorkflowRun.user_id == user.id,
                    WorkflowRun.deleted_at.is_(None),
                    WorkflowRun.type == WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE,
                    WorkflowRun.status.in_(["completed", "failed", "canceled"]),
                )
            )
        ).scalars().all()
    )
    now = _now()
    for run in rows:
        await _soft_delete_workflow_generated_images(
            db,
            run=run,
            deleted_at=now,
            cancel_message="model library job cleared",
        )
        run.deleted_at = now
        if run.conversation_id:
            conv = (
                await db.execute(
                    select(Conversation).where(
                        Conversation.id == run.conversation_id,
                        Conversation.user_id == user.id,
                        Conversation.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if conv is not None:
                conv.deleted_at = now
    await db.commit()
    return ApparelModelLibraryJobsClearOut(deleted=len(rows))


@router.post(
    "/apparel-model-library/jobs/{workflow_run_id}/items/{image_id}/save",
    response_model=ApparelModelLibraryItemOut,
    dependencies=[Depends(verify_csrf)],
)
async def save_apparel_model_library_job_item(
    workflow_run_id: str,
    image_id: str,
    body: ApparelModelLibrarySaveJobItemIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
    background_tasks: BackgroundTasks,
) -> ApparelModelLibraryItemOut:
    """从任务中心把一张产出图收藏到模特库。

    校验：workflow 属于当前用户；image_id 是该 workflow 任一 step 的产出。
    若 auto_tag=True，触发后台 vision 识别（不阻塞响应）。
    """
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    if run.type not in {WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE, WORKFLOW_TYPE}:
        raise _http(
            "invalid_workflow_type",
            "workflow type does not produce model images",
            400,
        )
    steps = await _load_steps(db, run.id)
    produced = await _workflow_produced_model_image_ids(
        db,
        user_id=user.id,
        steps=steps,
    )
    if image_id not in produced:
        raise _http("invalid_image", "image is not a product of this workflow", 404)

    item = await _add_user_library_item(
        db,
        user_id=user.id,
        source="generated",
        image_id=image_id,
        title=body.title,
        age_segment=body.age_segment,
        gender=body.gender,
        appearance_direction=body.appearance_direction,
        style_tags=body.style_tags,
    )
    await db.commit()
    item_id = str(item.get("id") or "")
    if body.auto_tag and item_id:
        # BackgroundTasks 在响应发出后再跑，避免阻塞用户。失败 graceful。
        background_tasks.add_task(_run_auto_tag_in_background, user.id, item_id)
    return _model_library_item_out(item)


def _merge_library_item_fields(
    *,
    existing: dict[str, Any],
    style_tags: list[str],
    appearance_direction: str | None,
    age_segment: str | None,
    gender: str | None,
    notes: str | None,
) -> dict[str, Any]:
    """vision 回写策略：style_tags 只追加去重，不覆盖用户手动选择；
    其他字段仅当 existing 里为空才填（保守不覆盖用户的手动值）。"""
    item = dict(existing)
    if style_tags:
        item["style_tags"] = _clean_style_tags(
            [*(item.get("style_tags") or []), *style_tags]
        )
    if appearance_direction and not _clean_optional_text(
        item.get("appearance_direction"), max_len=80
    ):
        item["appearance_direction"] = appearance_direction
    if age_segment:
        existing_age = _normalize_age_segment(item.get("age_segment"))
        if existing_age == "user_favorites":
            item["age_segment"] = age_segment
            item["library_folder"] = _model_library_folder_for_age(
                age_segment, item.get("gender")
            )
    if gender:
        existing_gender = _clean_optional_text(item.get("gender"), max_len=40)
        if not existing_gender:
            item["gender"] = gender
            item["library_folder"] = _model_library_folder_for_age(
                _normalize_age_segment(item.get("age_segment")), gender
            )
    if notes:
        item["auto_tag_notes"] = notes
    item["auto_tagged_at"] = _iso_now()
    item["updated_at"] = _iso_now()
    return item


async def _api_call_tagging_upstream(
    db: AsyncSession,
    *,
    image_id: str,
    user_id: str,
) -> dict[str, Any]:
    """API 进程内同步调 vision provider 做模特库自动打标签。

    [DECISION] worker 进程和 api 进程的 sys.path 隔离（docker-compose
    `working_dir` 不同），api 不能直接 import apps.worker.* 的模块。
    这里把"读图字节 + provider failover + httpx + JSON 解析"的精简版搬过来，
    避免再加一个共享 package。失败 graceful，返回 {} 让调用方留默认空字段。
    """
    import base64

    from lumen_core.providers import (
        DEFAULT_LEGACY_PROVIDER_BASE_URL,
        build_effective_provider_config,
        endpoint_kind_allowed,
        resolve_provider_proxy_url,
        weighted_priority_order,
    )
    from lumen_core.runtime_settings import get_spec

    from ..runtime_settings import get_setting

    image = (
        await db.execute(
            select(Image).where(
                Image.id == image_id,
                Image.user_id == user_id,
                Image.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if image is None:
        return {}
    storage_key = (image.storage_key or "").strip()
    if not storage_key:
        return {}
    try:
        path = _storage_path(storage_key)
        raw = path.read_bytes()
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "model_library auto_tag api: read image failed key=%s err=%s",
            storage_key,
            exc,
        )
        return {}
    if not raw:
        return {}
    mime = image.mime if isinstance(image.mime, str) and image.mime.startswith("image/") else "image/png"
    image_url = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    spec_providers = get_spec("providers")
    raw_providers = (
        await get_setting(db, spec_providers) if spec_providers else None
    )
    providers, _proxies, _errors = build_effective_provider_config(
        raw_providers=raw_providers,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    providers = [p for p in providers if endpoint_kind_allowed(p, "responses")]
    counters: dict[int, int] = {}
    ordered = weighted_priority_order(providers, counters)
    if not ordered:
        return {}

    instructions = (
        "你是模特库自动打标签助手。仔细分析这张模特图，输出严格 JSON。\n\n"
        "字段（全部必填，无法判断填空串/空数组）：\n"
        "- appearance_direction：英文小写之一：asian / east_asian / southeast_asian / "
        "south_asian / european / latin / middle_eastern / african / mixed / other。\n"
        "- style_tags：3-6 个中文短词，每个 ≤ 8 字，只写两类：\n"
        "    1) 相貌气质 — 五官 / 脸型 / 肤色 / 发型 / 骨相 / 整体观感"
        "（例：清冷、高颅顶、英气、邻家感、奶油感、骨相清秀、温柔、酷感）\n"
        "    2) 适合风格定位（例：少女感、高级感、知性、御姐感、复古、运动、文艺、街头）\n"
        "  禁止描述衣服 / 单品 / 拍摄场景 / 光线 / 品牌 / 营销词；禁止英文。\n"
        "- age_segment：toddler / child / teen / young_adult / adult / middle_aged / senior 之一。\n"
        "- gender：female 或 male 之一。\n"
        "- notes：≤ 60 字中文一句话，聚焦相貌与风格定位，不评价衣服。\n\n"
        "只输出 JSON 对象，不要 Markdown / 代码块 / 解释。字段必须用上述英文名。"
    )
    body = {
        "model": "gpt-5.4-mini",
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": instructions},
                    {"type": "input_image", "image_url": image_url},
                ],
            }
        ],
        "metadata": {"image_id": image_id, "purpose": "model_library_tagging"},
        "stream": False,
        "store": False,
        "max_output_tokens": 600,
    }
    last_err: str | None = None
    for provider in ordered:
        try:
            proxy_url = await resolve_provider_proxy_url(provider.proxy)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=25.0, write=25.0, pool=10.0),
                proxy=proxy_url,
            ) as client:
                resp = await client.post(
                    f"{provider.base_url.rstrip('/')}/v1/responses"
                    if not provider.base_url.rstrip("/").endswith("/v1")
                    else f"{provider.base_url.rstrip('/')}/responses",
                    json=body,
                    headers={
                        "authorization": f"Bearer {provider.api_key}",
                        "content-type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            last_err = f"network: {exc}"
            continue
        if resp.status_code >= 400:
            last_err = f"http {resp.status_code}"
            # 4xx 鉴权 / 模型不支持等：换号；5xx：换号
            continue
        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError):
            last_err = "bad_json"
            continue
        text_chunks: list[str] = []
        output = payload.get("output") if isinstance(payload, dict) else None
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    t = part.get("text") or part.get("output_text")
                    if isinstance(t, str) and t:
                        text_chunks.append(t)
        ot = payload.get("output_text") if isinstance(payload, dict) else None
        if isinstance(ot, str) and ot:
            text_chunks.append(ot)
        text = "".join(text_chunks).strip()
        return _parse_tagging_text(text)
    if last_err is not None:
        logger.info("model_library auto_tag api: all providers failed err=%s", last_err)
    return {}


def _parse_tagging_text(text: str) -> dict[str, Any]:
    if not text:
        return {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        nl = cleaned.find("\n")
        if nl != -1:
            cleaned = cleaned[nl + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
    payload: Any = None
    try:
        payload = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                payload = json.loads(match.group(0))
            except (json.JSONDecodeError, ValueError):
                payload = None
    if not isinstance(payload, dict):
        return {}
    return payload


_AGE_ALIASES_API: dict[str, str] = {
    "young": "young_adult",
    "youngadult": "young_adult",
    "young-adult": "young_adult",
    "kid": "child",
    "kids": "child",
    "baby": "toddler",
    "elder": "senior",
    "elderly": "senior",
    "old": "senior",
    "middleaged": "middle_aged",
    "middle-aged": "middle_aged",
    "teenager": "teen",
}


def _normalize_tagged_age(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower().replace(" ", "_")
    if key in MODEL_LIBRARY_AGE_SEGMENTS and key != "all":
        return key
    aliased = _AGE_ALIASES_API.get(key.replace("_", "")) or _AGE_ALIASES_API.get(key)
    return aliased


def _normalize_tagged_gender(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    aliases = {
        "female": "female",
        "woman": "female",
        "girl": "female",
        "f": "female",
        "male": "male",
        "man": "male",
        "boy": "male",
        "m": "male",
    }
    return aliases.get(value.strip().lower())


async def _auto_tag_library_item(
    *,
    db: AsyncSession,
    user_id: str,
    item_id: str,
) -> ApparelModelLibraryAutoTagOut:
    """Run vision tagging against one ``model_library_items`` row.

    Single-row UPDATE under transaction — concurrent auto-tag calls for
    different items don't trample each other (the JSON-file design read
    + wrote the whole user index for every call). When vision returns
    nothing usable we deliberately leave ``auto_tagged_at`` NULL so the
    UI can distinguish "not yet identified" from "identified but empty".
    """
    migrated_legacy = await _ensure_legacy_user_library_migrated(db, user_id)
    row = (
        await db.execute(
            select(ModelLibraryItem).where(
                ModelLibraryItem.id == item_id,
                ModelLibraryItem.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("not_found", "model library item not found", 404)
    image_id = (row.image_id or "").strip()
    if not image_id:
        raise _http("invalid_item", "library item has no backing image", 422)
    raw_payload = await _api_call_tagging_upstream(
        db, image_id=image_id, user_id=user_id
    )
    raw_tags_value = (
        raw_payload.get("style_tags")
        or raw_payload.get("tags")
        or raw_payload.get("styleTags")
        or []
    )
    if isinstance(raw_tags_value, str):
        raw_tags_iterable: list[str] = [raw_tags_value]
    elif isinstance(raw_tags_value, list):
        raw_tags_iterable = [
            str(t) for t in raw_tags_value if isinstance(t, (str, int, float))
        ]
    else:
        raw_tags_iterable = []
    style_tags = _clean_style_tags(raw_tags_iterable)
    appearance_direction = _clean_optional_text(
        raw_payload.get("appearance_direction")
        or raw_payload.get("appearanceDirection"),
        max_len=80,
    )
    age_segment = _normalize_tagged_age(
        raw_payload.get("age_segment") or raw_payload.get("ageSegment")
    )
    gender = _normalize_tagged_gender(raw_payload.get("gender"))
    notes = _clean_optional_text(raw_payload.get("notes"), max_len=200)

    # vision 全失败/空响应：保留用户原值不动，留 auto_tagged_at NULL。
    upstream_signal = bool(
        raw_payload
        and (style_tags or appearance_direction or age_segment or gender or notes)
    )
    if upstream_signal:
        if style_tags:
            row.style_tags = _clean_style_tags([*(row.style_tags or []), *style_tags])
        if appearance_direction and not row.appearance_direction:
            row.appearance_direction = appearance_direction
        if age_segment and _normalize_age_segment(row.age_segment) == "user_favorites":
            row.age_segment = age_segment
            row.library_folder = _model_library_folder_for_age(age_segment, row.gender)
        if gender and not row.gender:
            row.gender = gender
            row.library_folder = _model_library_folder_for_age(
                _normalize_age_segment(row.age_segment), gender
            )
        if notes:
            row.auto_tag_notes = notes
        row.auto_tagged_at = _now()
        await db.commit()
        await db.refresh(row)
    elif migrated_legacy:
        await db.commit()
    return ApparelModelLibraryAutoTagOut(
        item_id=item_id,
        style_tags=style_tags,
        appearance_direction=appearance_direction,
        age_segment=age_segment,  # type: ignore[arg-type]
        gender=gender,
        notes=notes,
    )


async def _run_auto_tag_in_background(user_id: str, item_id: str) -> None:
    """Background trigger for vision tagging. Uses its own DB session
    because it runs after the request response has been flushed.
    """
    try:
        from app.db import SessionLocal as _Session

        async with _Session() as session:
            await _auto_tag_library_item(
                db=session,
                user_id=user_id,
                item_id=item_id,
            )
    except HTTPException as exc:
        # Structured 404/422 (item gone / no backing image): expected, info level.
        logger.info(
            "model_library auto_tag background skipped user=%s item=%s status=%s",
            user_id,
            item_id,
            exc.status_code,
        )
    except Exception as exc:  # noqa: BLE001
        # Unexpected exceptions are real failures — surface to monitoring.
        logger.warning(
            "model_library auto_tag background failed user=%s item=%s err=%s",
            user_id,
            item_id,
            exc,
        )


@router.post(
    "/apparel-model-library/items/{item_id:path}/auto-tag",
    response_model=ApparelModelLibraryAutoTagOut,
    dependencies=[Depends(verify_csrf)],
)
async def auto_tag_apparel_model_library_item(
    item_id: str,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ApparelModelLibraryAutoTagOut:
    """同步触发 vision 自动识别，并把结果写回 library index。"""
    return await _auto_tag_library_item(db=db, user_id=user.id, item_id=item_id)


# ===========================================================================
# Poster Design Workflow（2026-05-12 起）
#
# 设计要点（与 apparel_model_showcase 同源蓝本）：
# 1. workflow_runs.type = "poster_design"；7 个 step：
#    copy_input → style_selection → copy_analysis → master_generation
#    → master_approval → multi_size_generation → delivery
#    （V1 删去 text_layer_editing + quality_review，全 AI 出图 + 文字直塞 prompt）
# 2. 文案分析走 Intent.VISION_QA（纯文本结构化，输出固定 schema JSON）
# 3. 母版生成走 Intent.TEXT_TO_IMAGE，N 个 candidate = N 个独立 Generation 任务
#    （MVP 默认 4 张），输出 1:1 母版（quality_mode=premium 时 fixed 2880x2880）
# 4. 多尺寸成品走 Intent.IMAGE_TO_IMAGE，把母版作为 reference，
#    每个 aspect = 独立 Generation 任务（不在单任务串行多尺寸，遵守 4K timeout 分层）
# 5. inpaint 返修走 Intent.IMAGE_TO_IMAGE + mask_image_id（用户传 mask），
#    prompt 在 worker 侧用 _wrap_inpaint_prompt 包装（OpenAI invariant 模板）
# 6. 风格 prompt 注入：从 PosterStyleItem.prompt_template 读，前缀化拼到母版 prompt
# 7. prompt cache friendly：所有 prompt 前缀稳定（风格 + 信息密度 + 母版指令固定），
#    用户具体文案在末尾
# ===========================================================================


POSTER_WORKFLOW_TYPE = "poster_design"
POSTER_WORKFLOW_STEPS = [
    "copy_input",
    "style_selection",
    "copy_analysis",
    "master_generation",
    "master_approval",
    "multi_size_generation",
    "delivery",
]
POSTER_DEFAULT_TARGET_ASPECTS: tuple[str, ...] = ("1:1", "9:16", "16:9", "3:4")
# 母版固定 1:1。premium 走 4K preset（2880x2880）；standard 走 size=auto。
POSTER_MASTER_ASPECT = "1:1"


# ---- size helpers ----------------------------------------------------------

# 用 _fixed_size_for_quality 已经覆盖了所有比例的 4K preset，
# 多尺寸成品按 quality_mode 选 4k / high。我们对接 apparel 的同一函数。


def _poster_image_params(
    *,
    aspect_ratio: str,
    quality_mode: str,
    count: int = 1,
) -> ImageParamsIn:
    """统一构造海报 ImageParamsIn。premium → final_quality='4k'。"""
    final_quality = "4k" if quality_mode == "premium" else "high"
    return _image_params(
        aspect_ratio=aspect_ratio,
        count=count,
        render_quality="high",
        final_quality=final_quality,
        fast=False,
    )


def _poster_master_image_params(quality_mode: str) -> ImageParamsIn:
    return _poster_image_params(
        aspect_ratio=POSTER_MASTER_ASPECT,
        quality_mode=quality_mode,
        count=1,
    )


async def _poster_find_preset_item(
    db: AsyncSession, *, user_id: str, style_id: str
) -> dict[str, Any] | None:
    from .poster_styles import _bootstrap_local_presets_if_empty, _find_preset_item

    await _bootstrap_local_presets_if_empty()
    return await _find_preset_item(db, user_id=user_id, item_id=style_id)


def _poster_style_from_preset(raw: dict[str, Any]) -> Any:
    class _PresetStyle:
        pass

    style = _PresetStyle()
    style.id = str(raw.get("id") or "")
    style.title = str(raw.get("title") or "")
    style.mood = str(raw.get("mood") or "")
    style.prompt_template = str(raw.get("prompt_template") or "")
    style.palette = [str(v) for v in (raw.get("palette") or []) if str(v).strip()]
    style.recommended_aspects = [
        str(v) for v in (raw.get("recommended_aspects") or []) if str(v).strip()
    ]
    style.style_tags = [
        str(v) for v in (raw.get("style_tags") or []) if str(v).strip()
    ]
    style.category = str(raw.get("category") or "")
    return style


async def _poster_load_style(
    db: AsyncSession,
    *,
    user_id: str,
    style_id: str,
) -> Any:
    """Load a poster style for workflow creation.

    User-created styles are private DB rows and must match ``user_id``. Presets
    live in the poster-style JSON index rather than ``poster_style_items``.
    """
    if style_id.startswith("preset:"):
        preset = await _poster_find_preset_item(db, user_id=user_id, style_id=style_id)
        if preset is not None:
            return _poster_style_from_preset(preset)
        raise _http("style_not_found", "poster style not found", 404)
    row = (
        await db.execute(
            select(PosterStyleItem).where(
                PosterStyleItem.id == style_id,
                PosterStyleItem.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http("style_not_found", "poster style not found", 404)
    return row


# ---- prompt helpers --------------------------------------------------------


def _poster_copy_analysis_prompt(copy_text: str) -> str:
    """文案语义切分 prompt（docs §8.1）。

    前缀（指令 + JSON schema 描述）稳定，用户输入只在末尾追加 —— 这样上游
    prompt cache 在多次重生间能命中前缀。
    """
    return (
        "你是海报文案结构化助手。请把下面一段海报营销文案切分成固定 JSON schema："
        "main_title（主标题，3-12 字）、subtitle（副标，可空）、selling_points（卖点数组，最多 4 条）、"
        "cta（行动号召，可空）、price（价格，可空）、tone（语气，1 句话）、"
        "info_density（信息密度，取值 high/medium/low）。"
        "必须只返回一个 JSON object，不要 Markdown、不要代码块、不要解释文字。"
        "如果某字段在原文里没有，填 null。info_density 的判定："
        "卖点+CTA+价格总条数 ≥ 4 → high；2-3 → medium；≤ 1 → low。"
        "保留原文措辞，不要改写或扩写。"
        f"\n\n原文案：\n{copy_text}"
    )


def _poster_style_summary(style: PosterStyleItem) -> dict[str, Any]:
    """把 PosterStyleItem 抽成稳定的 style_summary JSON，作为后续 prompt 注入字段。"""
    return {
        "style_id": style.id,
        "title": style.title or "",
        "mood": style.mood or "",
        "prompt_template": (style.prompt_template or "").strip(),
        "palette": list(style.palette or []),
        "recommended_aspects": list(style.recommended_aspects or []),
        "style_tags": list(style.style_tags or []),
        "category": style.category or "",
    }


def _poster_layout_safe_area(info_density: str) -> str:
    """按信息密度决定 safe area 位置；全 AI 出图也用这个信号控制构图留白。"""
    mapping = {
        "high": "下半区或左侧 1/3 区为主信息密集区，画面上半区留呼吸感",
        "medium": "中部水平带为主信息区，上下各留 25% 空间",
        "low": "中心 1/3 区为主信息区，四周大留白",
    }
    return mapping.get(info_density, mapping["medium"])


def _poster_text_fields_block(copy_analysis: dict[str, Any]) -> str:
    """把文案字段拼成稳定的 prompt 段（用于母版/多尺寸 prompt）。"""
    def _val(key: str) -> str:
        v = copy_analysis.get(key)
        if v is None:
            return ""
        if isinstance(v, list):
            return "、".join(str(x).strip() for x in v if str(x).strip())
        return str(v).strip()

    main_title = _val("main_title")
    subtitle = _val("subtitle")
    selling_points = _val("selling_points")
    cta = _val("cta")
    price = _val("price")
    lines = []
    if main_title:
        lines.append(f"- main_title: {main_title}")
    if subtitle:
        lines.append(f"- subtitle: {subtitle}")
    if selling_points:
        lines.append(f"- selling_points: {selling_points}")
    if cta:
        lines.append(f"- cta: {cta}")
    if price:
        lines.append(f"- price: {price}")
    return "\n".join(lines) if lines else "- main_title: (无)"


def _poster_brand_assets_block(brand_assets: dict[str, Any]) -> str:
    """品牌资产 prompt 段；空字段直接跳过，保持 prompt 前缀稳定。"""
    primary_color = str(brand_assets.get("primary_color") or "").strip()
    font_family = str(brand_assets.get("font_family") or "").strip()
    bits: list[str] = []
    if primary_color:
        bits.append(f"primary brand color: {primary_color}")
    if font_family:
        bits.append(f"preferred font family: {font_family}")
    if brand_assets.get("logo_image_id"):
        bits.append("a brand logo image is provided as reference; integrate it tastefully if appropriate")
    if brand_assets.get("product_image_id"):
        bits.append("a product image is provided as reference; place it as the visual focal point")
    return "; ".join(bits) if bits else "no extra brand asset constraints"


def _poster_master_prompt(
    *,
    style_summary: dict[str, Any],
    copy_analysis: dict[str, Any],
    brand_assets: dict[str, Any],
    candidate_index: int,
) -> str:
    """母版 prompt（docs §8.2 改良）。

    决策：全 AI 出图——main_title / subtitle / cta / price 当字段塞进 prompt，
    让 gpt-image-2 直接画带文字的成品。短中文（3-8 字）实测可用，长文走 inpaint 兜底。
    prompt 前缀稳定（指令 + 风格段），candidate_index / 用户文案在末尾。
    """
    info_density = str(copy_analysis.get("info_density") or "medium")
    palette = style_summary.get("palette") or []
    palette_text = ", ".join(str(p) for p in palette if str(p).strip()) or "balanced palette"
    style_prompt_template = (style_summary.get("prompt_template") or "").strip()
    style_mood = (style_summary.get("mood") or "").strip()
    safe_area = _poster_layout_safe_area(info_density)
    text_block = _poster_text_fields_block(copy_analysis)
    brand_block = _poster_brand_assets_block(brand_assets)
    style_block = style_prompt_template or "clean modern poster design"
    return (
        "Create one high-quality marketing poster master, square 1:1 composition, "
        "print-ready visual.\n"
        "This is a master candidate used to confirm the visual style before "
        "rendering other aspect ratios; keep composition logic clean.\n"
        "Render the marketing text fields directly inside the image (do NOT leave "
        "them as placeholders): main_title is the largest, subtitle smaller below, "
        "selling_points as short bullets, cta as a small accent badge, price as a "
        "highlighted callout if present. Keep all text short, sharp, and legible.\n"
        f"Style direction: {style_block}.\n"
        f"Color palette priority: {palette_text}.\n"
        f"Mood: {style_mood or 'aligned with the style direction above'}.\n"
        f"Information density: {info_density}; layout safe area: {safe_area}.\n"
        f"Brand assets: {brand_block}.\n"
        "Avoid: watermark, signature, busy textures over text, unreadable glyphs, "
        "duplicated headlines, English filler text when source copy is Chinese.\n"
        f"Text fields to render:\n{text_block}\n"
        f"Candidate variation number: {candidate_index}."
    )


def _poster_render_prompt(
    *,
    style_summary: dict[str, Any],
    copy_analysis: dict[str, Any],
    target_aspect: str,
    adjustments: str = "",
) -> str:
    """多尺寸 prompt（docs §8.3）。母版作为 reference 重出目标比例。"""
    palette = style_summary.get("palette") or []
    palette_text = ", ".join(str(p) for p in palette if str(p).strip()) or "balanced palette"
    info_density = str(copy_analysis.get("info_density") or "medium")
    safe_area = _poster_layout_safe_area(info_density)
    text_block = _poster_text_fields_block(copy_analysis)
    extra = (adjustments or "").strip()
    extra_line = f"\nAdditional direction: {extra}" if extra else ""
    return (
        f"Re-render the reference poster master into a {target_aspect} composition.\n"
        "Match the visual style, color palette, mood, decoration logic, and text "
        "rendering style of the reference image exactly.\n"
        "Adapt the composition naturally to the new aspect ratio without distortion; "
        "reposition text fields to keep them clearly legible in the new frame.\n"
        f"Reference palette: {palette_text}.\n"
        f"Information density: {info_density}; layout safe area: {safe_area}.\n"
        f"Text fields to keep visible:\n{text_block}\n"
        "Do not change the wording of any text field; only adjust position, size, "
        "and orientation to fit the new aspect ratio."
        f"{extra_line}"
    )


def _poster_revision_prompt(
    *,
    style_summary: dict[str, Any],
    copy_analysis: dict[str, Any],
    target_aspect: str,
    instruction: str,
    scope: str,
) -> str:
    """整张返修 prompt（scope=background 或 style）。inpaint 走单独的路径。"""
    if scope == "style":
        return (
            f"{_poster_render_prompt(style_summary=style_summary, copy_analysis=copy_analysis, target_aspect=target_aspect)}"
            f"\nUser revision (style change): {instruction.strip()}."
        )
    # background: 默认保留风格+文案，只改背景/构图
    return (
        f"Revise this poster background while keeping the {target_aspect} composition.\n"
        "Preserve the visual style, color palette, mood, and decoration logic of the reference exactly.\n"
        "Do not change the wording of any text field; only adjust the background, "
        "layout, or composition based on the user's instruction.\n"
        f"Text fields to keep visible:\n{_poster_text_fields_block(copy_analysis)}\n"
        f"User revision: {instruction.strip()}."
    )


# ---- step / state helpers --------------------------------------------------


def _poster_seed_steps(run: WorkflowRun) -> list[WorkflowStep]:
    """初始化 7 个 step：copy_input/style_selection 在创建时即 approved（用户已选定），
    copy_analysis 进入 running，其它 step 处于 waiting_input。"""
    steps: list[WorkflowStep] = []
    for key in POSTER_WORKFLOW_STEPS:
        status = "waiting_input"
        input_json: dict[str, Any] = {}
        output_json: dict[str, Any] = {}
        if key == "copy_input":
            status = "approved"
            input_json = {"copy_text": run.user_prompt}
            output_json = {"confirmed": True}
        elif key == "style_selection":
            status = "approved"
            input_json = {
                "style_id": (run.metadata_jsonb or {}).get("style_id"),
                "target_aspects": (run.metadata_jsonb or {}).get("target_aspects") or list(POSTER_DEFAULT_TARGET_ASPECTS),
            }
            output_json = {"confirmed": True}
        elif key == "copy_analysis":
            status = "running"
            input_json = {
                "copy_text": run.user_prompt,
                "prompt_contract": "extract poster copy into structured JSON",
            }
        steps.append(
            WorkflowStep(
                workflow_run_id=run.id,
                step_key=key,
                status=status,
                input_json=input_json,
                output_json=output_json,
            )
        )
    return steps


async def _create_poster_workflow_task(
    *,
    db: AsyncSession,
    user: User,
    conv: Conversation,
    intent: Intent,
    text: str,
    attachment_ids: list[str],
    idempotency_key: str,
    workflow_run_id: str,
    workflow_step_key: str,
    image_params: ImageParamsIn | None = None,
    chat_params: ChatParamsIn | None = None,
    workflow_meta: dict[str, Any] | None = None,
    mask_image_id: str | None = None,
) -> tuple[_PublishBundle, str | None, list[str]]:
    """与 _create_workflow_task 同源；额外支持 mask_image_id（inpaint）+
    workflow_type=poster_design 标记。"""
    user_msg = Message(
        conversation_id=conv.id,
        role=Role.USER.value,
        content={
            "text": text,
            "attachments": [{"image_id": image_id} for image_id in attachment_ids],
            "workflow_run_id": workflow_run_id,
            "workflow_step_key": workflow_step_key,
        },
        intent=None,
        status=None,
    )
    db.add(user_msg)
    await db.flush()

    result = await _create_assistant_task(
        db=db,
        user_id=user.id,
        account_mode=getattr(user, "account_mode", "wallet"),
        conv=conv,
        user_msg=user_msg,
        intent=intent,
        idempotency_key=idempotency_key[:64],
        image_params=image_params or ImageParamsIn(),
        chat_params=chat_params or ChatParamsIn(),
        system_prompt=None,
        attachment_ids=attachment_ids,
        text=text,
        mask_image_id=mask_image_id,
    )

    meta = {
        "workflow_run_id": workflow_run_id,
        "workflow_type": POSTER_WORKFLOW_TYPE,
        "workflow_step_key": workflow_step_key,
        **(workflow_meta or {}),
    }
    if result.completion_id:
        comp = await db.get(Completion, result.completion_id)
        if comp is not None:
            req = dict(comp.upstream_request or {})
            req.update(meta)
            comp.upstream_request = req
    for generation_id in result.generation_ids:
        gen = await db.get(Generation, generation_id)
        if gen is not None:
            req = dict(gen.upstream_request or {})
            req.update(meta)
            gen.upstream_request = req

    bundle = _PublishBundle(
        assistant_msg_id=result.assistant_msg.id,
        message_ids=[user_msg.id, result.assistant_msg.id],
        outbox_payloads=result.outbox_payloads,
        outbox_rows=result.outbox_rows,
    )
    return bundle, result.completion_id, result.generation_ids


def _poster_parse_copy_analysis_text(text: str) -> dict[str, Any]:
    """解析文案分析 completion 的返回 JSON，规整字段；解析失败时 graceful 降级。

    与 apparel _try_parse_json_text 不同——后者会走 _normalize_product_analysis_payload
    把字段规整到 apparel schema，把海报字段全丢掉。这里只做原始 JSON 提取 + 海报字段规整。
    """
    raw = (text or "").strip()
    parsed: Any = None
    if raw:
        body = raw
        if body.startswith("```"):
            body = body.strip("`")
            body = body.removeprefix("json").strip()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            start = body.find("{")
            end = body.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(body[start : end + 1])
                except json.JSONDecodeError:
                    parsed = None
    if not isinstance(parsed, dict):
        parsed = {}
    main_title = parsed.get("main_title")
    subtitle = parsed.get("subtitle")
    selling_points = parsed.get("selling_points")
    cta = parsed.get("cta")
    price = parsed.get("price")
    tone = parsed.get("tone")
    info_density = parsed.get("info_density")
    if info_density not in {"high", "medium", "low"}:
        info_density = "medium"
    return {
        "main_title": str(main_title).strip() if main_title else None,
        "subtitle": str(subtitle).strip() if subtitle else None,
        "selling_points": (
            _clean_string_list(
                (str(item) for item in selling_points) if isinstance(selling_points, list) else [],
                max_items=4,
                max_len=60,
            )
            if isinstance(selling_points, list)
            else []
        ),
        "cta": str(cta).strip() if cta else None,
        "price": str(price).strip() if price else None,
        "tone": str(tone).strip() if tone else None,
        "info_density": info_density,
        "raw_text": text or "",
    }


def _poster_merge_copy_corrections(
    base: dict[str, Any],
    corrections: dict[str, Any],
) -> dict[str, Any]:
    """用户对文案分析的手工修正——None 表示沿用 AI 输出，非 None 覆盖。"""
    final = dict(base or {})
    raw = corrections if isinstance(corrections, dict) else {}
    for key, value in raw.items():
        if value is not None:
            final[key] = value
    final["user_corrections"] = raw
    final["confirmed_at"] = _now().isoformat()
    return final


async def _sync_poster_workflow_outputs(
    db: AsyncSession,
    run: WorkflowRun,
) -> None:
    """与 _sync_workflow_outputs 同源——按步骤推进状态机：

    - copy_analysis running + completion 完成 → needs_review（用户修正后 approve）
    - master_generation running + 任一 master 完成 → 标记母版 status=ready
    - master_generation 所有 master ready/failed → needs_review（用户选 1 张）
    - multi_size_generation running + render 完成 → 标记 render status=ready
    - multi_size_generation 所有 aspect 都有 ready 图 → needs_review / completed
    """
    if run.type != POSTER_WORKFLOW_TYPE:
        return
    steps = {step.step_key: step for step in await _load_steps(db, run.id)}

    # ----- copy_analysis -----
    copy_step = steps.get("copy_analysis")
    if copy_step and copy_step.status == "running" and copy_step.task_ids:
        completion = (
            await db.execute(
                select(Completion)
                .where(Completion.id.in_(copy_step.task_ids))
                .order_by(desc(Completion.created_at))
                .limit(1)
            )
        ).scalar_one_or_none()
        if completion is not None:
            if completion.status == CompletionStatus.SUCCEEDED.value:
                copy_step.output_json = _poster_parse_copy_analysis_text(completion.text)
                copy_step.status = "needs_review"
                run.status = "needs_review"
                run.current_step = "copy_analysis"
            elif completion.status == CompletionStatus.FAILED.value:
                copy_step.status = "failed"
                copy_step.output_json = {
                    "error_code": completion.error_code,
                    "error_message": completion.error_message,
                }
                run.status = "failed"

    # ----- master_generation -----
    masters = list(
        (
            await db.execute(
                select(PosterMaster)
                .where(PosterMaster.workflow_run_id == run.id)
                .order_by(PosterMaster.candidate_index.asc())
            )
        ).scalars().all()
    )
    if masters:
        all_master_task_ids = [task_id for master in masters for task_id in (master.task_ids or [])]
        gens_by_id: dict[str, Generation] = {}
        images_by_gen: dict[str, Image] = {}
        if all_master_task_ids:
            master_generations = (
                await db.execute(
                    select(Generation).where(Generation.id.in_(all_master_task_ids))
                )
            ).scalars().all()
            gens_by_id = {g.id: g for g in master_generations}
            images = (
                await db.execute(
                    select(Image)
                    .where(
                        Image.owner_generation_id.in_([g.id for g in master_generations]),
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            ).scalars().all()
            for image in images:
                if image.owner_generation_id and image.owner_generation_id not in images_by_gen:
                    images_by_gen[image.owner_generation_id] = image

        for master in masters:
            if master.image_id is None:
                # 取第一个 task 对应的图
                for task_id in master.task_ids or []:
                    image = images_by_gen.get(task_id)
                    if image is not None:
                        master.image_id = image.id
                        break
            if master.image_id and master.status == "generating":
                master.status = "ready"
            elif (
                master.status == "generating"
                and master.task_ids
                and all(
                    gens_by_id.get(task_id) is not None
                    and gens_by_id[task_id].status == GenerationStatus.FAILED.value
                    for task_id in master.task_ids
                )
            ):
                master.status = "failed"

        master_step = steps.get("master_generation")
        if master_step and master_step.status == "running":
            current_task_ids = {
                task_id for task_id in (master_step.task_ids or []) if isinstance(task_id, str)
            }
            current_masters = [
                m
                for m in masters
                if not current_task_ids
                or current_task_ids.intersection(set(m.task_ids or []))
            ]
            ready_count = sum(1 for m in current_masters if m.status == "ready")
            failed_count = sum(1 for m in current_masters if m.status == "failed")
            expected = int(
                (master_step.input_json or {}).get("candidate_count")
                or len(current_masters)
                or len(masters)
            )
            if ready_count >= max(1, expected):
                master_step.status = "needs_review"
                master_step.image_ids = _dedupe_nonempty(
                    m.image_id for m in current_masters if isinstance(m.image_id, str)
                )
                run.current_step = "master_approval"
                run.status = "needs_review"
                approval_step = steps.get("master_approval")
                if approval_step and approval_step.status == "waiting_input":
                    approval_step.status = "needs_review"
            elif failed_count and failed_count == len(current_masters):
                master_step.status = "failed"
                failed_generations = [
                    g
                    for g in gens_by_id.values()
                    if g.status == GenerationStatus.FAILED.value
                    and (not current_task_ids or g.id in current_task_ids)
                ]
                master_step.output_json = {
                    **(master_step.output_json or {}),
                    "failed_generation_ids": [g.id for g in failed_generations],
                    "error_message": _task_error_summary(failed_generations, "母版生成失败"),
                }
                run.status = "failed"
                run.current_step = "master_generation"

    # ----- multi_size_generation -----
    renders = list(
        (
            await db.execute(
                select(PosterRender)
                .where(PosterRender.workflow_run_id == run.id)
                .order_by(PosterRender.created_at.asc(), PosterRender.id.asc())
            )
        ).scalars().all()
    )
    if renders:
        all_render_task_ids = [task_id for r in renders for task_id in (r.task_ids or [])]
        render_gens_by_id: dict[str, Generation] = {}
        render_images_by_gen: dict[str, Image] = {}
        if all_render_task_ids:
            render_generations = (
                await db.execute(
                    select(Generation).where(Generation.id.in_(all_render_task_ids))
                )
            ).scalars().all()
            render_gens_by_id = {g.id: g for g in render_generations}
            render_images = (
                await db.execute(
                    select(Image)
                    .where(
                        Image.owner_generation_id.in_([g.id for g in render_generations]),
                        Image.deleted_at.is_(None),
                    )
                    .order_by(Image.created_at.asc(), Image.id.asc())
                )
            ).scalars().all()
            for image in render_images:
                if image.owner_generation_id and image.owner_generation_id not in render_images_by_gen:
                    render_images_by_gen[image.owner_generation_id] = image
        multi_step = steps.get("multi_size_generation")
        active_task_ids: set[str] = set()
        if multi_step and multi_step.status == "running":
            raw_active_task_ids = (multi_step.input_json or {}).get("active_task_ids")
            if isinstance(raw_active_task_ids, list):
                active_task_ids = {
                    task_id
                    for task_id in raw_active_task_ids
                    if isinstance(task_id, str) and task_id
                }
        for render in renders:
            # 整张返修也会 append task_id；image_id 取最新成功的
            render_task_ids = [
                task_id for task_id in (render.task_ids or []) if isinstance(task_id, str)
            ]
            task_ids_for_status = render_task_ids
            if render.status in {"generating", "revising"} and active_task_ids:
                active_for_render = [
                    task_id for task_id in render_task_ids if task_id in active_task_ids
                ]
                if active_for_render:
                    task_ids_for_status = active_for_render

            latest_image_id: str | None = None
            for task_id in task_ids_for_status:
                image = render_images_by_gen.get(task_id)
                if image is not None:
                    latest_image_id = image.id
            if latest_image_id and latest_image_id != render.image_id:
                render.image_id = latest_image_id
            if latest_image_id and render.status in {"generating", "revising"}:
                render.status = "ready"
            elif (
                render.status in {"generating", "revising"}
                and task_ids_for_status
                and all(
                    render_gens_by_id.get(task_id) is not None
                    and render_gens_by_id[task_id].status == GenerationStatus.FAILED.value
                    for task_id in task_ids_for_status
                )
            ):
                render.status = "failed"

        if multi_step and multi_step.status == "running":
            current_renders = (
                [
                    r
                    for r in renders
                    if active_task_ids.intersection(
                        task_id
                        for task_id in (r.task_ids or [])
                        if isinstance(task_id, str)
                    )
                ]
                if active_task_ids
                else renders
            )
            ready_count = sum(1 for r in current_renders if r.status == "ready")
            failed_count = sum(1 for r in current_renders if r.status == "failed")
            raw_expected = int(
                (multi_step.input_json or {}).get("expected_render_count")
                or len(current_renders)
                or len(renders)
            )
            expected = min(raw_expected, len(current_renders) or raw_expected)
            multi_step.image_ids = _dedupe_nonempty(
                r.image_id for r in renders if isinstance(r.image_id, str)
            )
            if ready_count >= max(1, expected):
                multi_step.status = "needs_review"
                run.current_step = "multi_size_generation"
                run.status = "needs_review"
            elif failed_count and failed_count == len(current_renders):
                multi_step.status = "failed"
                failed_generations = [
                    g
                    for g in render_gens_by_id.values()
                    if g.status == GenerationStatus.FAILED.value
                    and (not active_task_ids or g.id in active_task_ids)
                ]
                multi_step.output_json = {
                    **(multi_step.output_json or {}),
                    "failed_generation_ids": [g.id for g in failed_generations],
                    "error_message": _task_error_summary(failed_generations, "多尺寸生成失败"),
                }
                run.status = "failed"
                run.current_step = "multi_size_generation"
        elif multi_step and multi_step.status in {"needs_review", "completed"}:
            # 后续返修也刷新 image_ids；
            multi_step.image_ids = _dedupe_nonempty(
                r.image_id for r in renders if isinstance(r.image_id, str)
            )


# ---- endpoints -------------------------------------------------------------


@router.post(
    "/poster-design",
    response_model=PosterDesignWorkflowCreateOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_poster_design_workflow(
    body: PosterDesignWorkflowCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PosterDesignWorkflowCreateOut:
    """创建海报工作流 + 触发文案分析。

    流程：
    1. 校验 copy_text 非空（pydantic 已校验 min_length=1）
    2. 校验 style_id 存在（_poster_load_style）
    3. 可选校验 brand_assets 中 logo/product image_id 归属当前用户
    4. 创建 WorkflowRun + 7 个 step（copy_input/style_selection 直接 approved）
    5. 入队 vision_qa 任务做文案切分
    """
    copy_text = (body.copy_text or "").strip()
    if not copy_text:
        raise _http("missing_copy_text", "copy_text is required", 422)
    style = await _poster_load_style(db, user_id=user.id, style_id=body.style_id)
    brand_image_ids: list[str] = []
    if body.brand_assets.logo_image_id:
        brand_image_ids.append(body.brand_assets.logo_image_id)
    if body.brand_assets.product_image_id:
        brand_image_ids.append(body.brand_assets.product_image_id)
    if brand_image_ids:
        await _validate_owned_images(
            db,
            user_id=user.id,
            image_ids=brand_image_ids,
            min_count=1,
            max_count=8,
        )

    title = (body.title or "").strip() or (copy_text[:24] or "海报设计")
    conv = await _get_or_create_workflow_conversation(
        db,
        user=user,
        conversation_id=body.conversation_id,
        title=title,
        workflow_type=POSTER_WORKFLOW_TYPE,
    )
    conv.title = title
    conv.archived = True
    run = WorkflowRun(
        conversation_id=conv.id,
        user_id=user.id,
        type=POSTER_WORKFLOW_TYPE,
        status="running",
        title=title,
        user_prompt=copy_text,
        product_image_ids=brand_image_ids,  # 复用字段承载品牌资产图（前端按 type 解释）
        current_step="copy_analysis",
        quality_mode=body.quality_mode,
        metadata_jsonb={
            "template": POSTER_WORKFLOW_TYPE,
            "style_id": style.id,
            "style_summary": _poster_style_summary(style),
            "target_aspects": list(body.target_aspects),
            "brand_assets": body.brand_assets.model_dump(),
        },
    )
    db.add(run)
    await db.flush()
    for step in _poster_seed_steps(run):
        db.add(step)
    copy_step = await _step(db, run.id, "copy_analysis")
    bundle, completion_id, _ = await _create_poster_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.VISION_QA,
        text=_poster_copy_analysis_prompt(copy_text),
        attachment_ids=[],  # vision_qa 纯文本结构化也走 vision route，无图 attachment
        idempotency_key=f"wf:{run.id}:copy",
        workflow_run_id=run.id,
        workflow_step_key="copy_analysis",
        chat_params=ChatParamsIn(reasoning_effort="low", stream=True),
        workflow_meta={"workflow_action": "poster_copy_analysis"},
    )
    copy_step.task_ids = [completion_id] if completion_id else []
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    return PosterDesignWorkflowCreateOut(
        workflow_run_id=run.id,
        status=run.status,
        current_step=run.current_step,
    )


@router.post(
    "/{workflow_run_id}/steps/copy-analysis/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_copy_analysis(
    workflow_run_id: str,
    body: CopyAnalysisApproveIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    """用户确认（含手工修正）文案分析输出，推进到 master_generation 等待入参。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await _sync_poster_workflow_outputs(db, run)
    copy_step = await _step(db, run.id, "copy_analysis")
    if copy_step.status not in {"needs_review", "approved"}:
        raise _http("step_not_ready", "copy analysis is not ready to approve", 409)
    copy_step.output_json = _poster_merge_copy_corrections(
        copy_step.output_json or {},
        body.corrections or {},
    )
    copy_step.status = "approved"
    copy_step.approved_at = _now()
    copy_step.approved_by = user.id
    master_step = await _step(db, run.id, "master_generation")
    if master_step.status == "waiting_input":
        master_step.input_json = {
            "copy_analysis": copy_step.output_json,
            "style_summary": (run.metadata_jsonb or {}).get("style_summary") or {},
        }
    run.current_step = "master_generation"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/masters",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_poster_masters(
    workflow_run_id: str,
    body: PosterMastersCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    """生成 N 张母版候选（默认 4），每张 = 独立 Generation 任务。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await _sync_poster_workflow_outputs(db, run)
    copy_step = await _step(db, run.id, "copy_analysis")
    if copy_step.status != "approved":
        raise _http("copy_not_approved", "approve copy analysis first", 409)
    master_step = await _step(db, run.id, "master_generation")
    if master_step.status == "running":
        raise _http("already_running", "master generation already running", 409)

    style_summary = (run.metadata_jsonb or {}).get("style_summary") or {}
    brand_assets = (run.metadata_jsonb or {}).get("brand_assets") or {}
    copy_analysis = copy_step.output_json or {}
    candidate_count = max(1, min(8, body.candidate_count))

    # 已有 master 行：累加 candidate_index 避免唯一冲突。
    existing_masters = (
        await db.execute(
            select(PosterMaster)
            .where(PosterMaster.workflow_run_id == run.id)
            .order_by(PosterMaster.candidate_index.asc())
        )
    ).scalars().all()
    existing_count = len(existing_masters)

    master_step.status = "running"
    master_step.input_json = {
        "candidate_count": candidate_count,
        "size_mode": body.size_mode,
        "size": body.size,
        "copy_analysis": copy_analysis,
        "style_summary": style_summary,
    }
    run.current_step = "master_generation"
    run.status = "running"

    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    image_params = _poster_master_image_params(run.quality_mode)
    if body.size_mode == "fixed" and body.size:
        image_params = image_params.model_copy(
            update={"size_mode": "fixed", "fixed_size": body.size}
        )

    for idx in range(1, candidate_count + 1):
        candidate_index = existing_count + idx
        master = PosterMaster(
            workflow_run_id=run.id,
            candidate_index=candidate_index,
            status="generating",
            style_summary_json={
                "style_summary": style_summary,
                "copy_analysis": copy_analysis,
                "candidate_index": candidate_index,
            },
        )
        db.add(master)
        await db.flush()
        bundle, _, gen_ids = await _create_poster_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=Intent.TEXT_TO_IMAGE,
            text=_poster_master_prompt(
                style_summary=style_summary,
                copy_analysis=copy_analysis,
                brand_assets=brand_assets,
                candidate_index=candidate_index,
            ),
            attachment_ids=[],
            idempotency_key=f"wf:{run.id[:22]}:m:{candidate_index}",
            workflow_run_id=run.id,
            workflow_step_key="master_generation",
            image_params=image_params,
            workflow_meta={
                "workflow_action": "poster_master",
                "workflow_master_id": master.id,
                "workflow_master_index": candidate_index,
            },
        )
        master.task_ids = gen_ids
        task_ids.extend(gen_ids)
        bundles.append(bundle)
    master_step.task_ids = _dedupe_nonempty(task_ids)
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/masters/{master_id}/approve",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def approve_poster_master(
    workflow_run_id: str,
    master_id: str,
    body: PosterMasterApproveIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    """用户选定 1 张母版。其它候选 status 保留 ready，但 selected 字段只有 1 张。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await _sync_poster_workflow_outputs(db, run)
    master = (
        await db.execute(
            select(PosterMaster).where(
                PosterMaster.id == master_id,
                PosterMaster.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if master is None:
        raise _http("not_found", "poster master not found", 404)
    if master.status != "ready" or not master.image_id:
        raise _http("master_not_ready", "poster master is not ready to approve", 409)
    # 把其它已选的 master 切回 ready，保证只有 1 张 selected
    other_selected = (
        await db.execute(
            select(PosterMaster).where(
                PosterMaster.workflow_run_id == run.id,
                PosterMaster.status == "selected",
                PosterMaster.id != master.id,
            )
        )
    ).scalars().all()
    for row in other_selected:
        row.status = "ready"
        row.selected_at = None
    master.status = "selected"
    master.selected_at = _now()
    master_step = await _step(db, run.id, "master_generation")
    if master_step.status == "needs_review":
        master_step.status = "approved"
        master_step.approved_at = _now()
        master_step.approved_by = user.id
        master_step.output_json = {
            **(master_step.output_json or {}),
            "selected_master_id": master.id,
            "selected_master_image_id": master.image_id,
            "adjustments": body.adjustments or "",
        }
    approval_step = await _step(db, run.id, "master_approval")
    approval_step.status = "approved"
    approval_step.approved_at = _now()
    approval_step.approved_by = user.id
    approval_step.input_json = {
        **(approval_step.input_json or {}),
        "selected_master_id": master.id,
        "selected_master_image_id": master.image_id,
        "adjustments": body.adjustments or "",
    }
    approval_step.output_json = {
        "selected_master_id": master.id,
        "selected_master_image_id": master.image_id,
    }
    multi_step = await _step(db, run.id, "multi_size_generation")
    if multi_step.status == "waiting_input":
        multi_step.input_json = {
            **(multi_step.input_json or {}),
            "selected_master_id": master.id,
            "selected_master_image_id": master.image_id,
            "target_aspects": (run.metadata_jsonb or {}).get("target_aspects")
                or list(POSTER_DEFAULT_TARGET_ASPECTS),
        }
    run.current_step = "multi_size_generation"
    run.status = "needs_review"
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def _poster_selected_master(db: AsyncSession, run_id: str) -> PosterMaster:
    master = (
        await db.execute(
            select(PosterMaster).where(
                PosterMaster.workflow_run_id == run_id,
                PosterMaster.status == "selected",
            )
        )
    ).scalar_one_or_none()
    if master is None:
        raise _http("master_not_selected", "select a poster master first", 409)
    if not master.image_id:
        raise _http("master_missing_image", "selected master has no image", 409)
    return master


@router.post(
    "/{workflow_run_id}/renders",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def create_poster_renders(
    workflow_run_id: str,
    body: PosterRendersCreateIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    """按 aspect 批量生成多尺寸成品。每个 aspect = 独立 Generation 任务（stagger 入队）。

    复用现有 _create_assistant_task 内部的 stagger（i*5s, cap 30s），
    但因为每次都 count=1，stagger 跨调用不会触发——这与 apparel showcase 同。
    """
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await _sync_poster_workflow_outputs(db, run)
    master = await _poster_selected_master(db, run.id)
    multi_step = await _step(db, run.id, "multi_size_generation")
    if multi_step.status == "running":
        raise _http("already_running", "multi-size generation already running", 409)
    aspects = list(dict.fromkeys(body.aspects))
    if not aspects:
        raise _http("missing_aspects", "at least one aspect ratio required", 422)
    style_summary = (run.metadata_jsonb or {}).get("style_summary") or {}
    copy_step = await _step(db, run.id, "copy_analysis")
    copy_analysis = copy_step.output_json or {}
    adjustments = str(
        (await _step(db, run.id, "master_approval")).output_json.get("adjustments") or ""
    ).strip()
    quality_mode = body.quality_mode if body.quality_mode in {"standard", "premium"} else run.quality_mode

    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    # 已有 render 行（同 aspect 已生成过则跳过，避免唯一冲突）
    existing_renders = (
        await db.execute(
            select(PosterRender).where(PosterRender.workflow_run_id == run.id)
        )
    ).scalars().all()
    existing_aspects = {r.aspect_ratio for r in existing_renders}
    pending_aspects = [aspect for aspect in aspects if aspect not in existing_aspects]

    multi_step.status = "running"
    multi_step.input_json = {
        **(multi_step.input_json or {}),
        "aspects": aspects,
        "use_master_as_reference": body.use_master_as_reference,
        "quality_mode": quality_mode,
        "expected_render_count": len(pending_aspects),
        "active_aspects": pending_aspects,
        "active_task_ids": [],
    }
    run.current_step = "multi_size_generation"
    run.status = "running"

    if not pending_aspects:
        requested_image_ids = _dedupe_nonempty(
            r.image_id
            for r in existing_renders
            if r.aspect_ratio in aspects and isinstance(r.image_id, str)
        )
        if not requested_image_ids:
            raise _http(
                "renders_already_exist",
                "requested renders already exist but are not ready",
                409,
            )
        multi_step.status = "needs_review"
        multi_step.image_ids = requested_image_ids
        run.status = "needs_review"
        await db.commit()
        run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
        out = await _build_run_out(db, run)
        await db.commit()
        return out

    bundles: list[_PublishBundle] = []
    task_ids: list[str] = []
    for idx, aspect in enumerate(pending_aspects, start=1):
        image_params = _poster_image_params(
            aspect_ratio=aspect, quality_mode=quality_mode, count=1
        )
        ref_ids = [master.image_id] if body.use_master_as_reference and master.image_id else []
        size_str = image_params.fixed_size or "auto"
        render = PosterRender(
            workflow_run_id=run.id,
            master_id=master.id,
            aspect_ratio=aspect,
            size=size_str,
            status="generating",
            metadata_jsonb={
                "quality_mode": quality_mode,
                "use_master_as_reference": body.use_master_as_reference,
            },
        )
        db.add(render)
        await db.flush()
        bundle, _, gen_ids = await _create_poster_workflow_task(
            db=db,
            user=user,
            conv=conv,
            intent=Intent.IMAGE_TO_IMAGE if ref_ids else Intent.TEXT_TO_IMAGE,
            text=_poster_render_prompt(
                style_summary=style_summary,
                copy_analysis=copy_analysis,
                target_aspect=aspect,
                adjustments=adjustments,
            ),
            attachment_ids=ref_ids,
            idempotency_key=f"wf:{run.id[:18]}:r:{idx}:{aspect}",
            workflow_run_id=run.id,
            workflow_step_key="multi_size_generation",
            image_params=image_params,
            workflow_meta={
                "workflow_action": "poster_render",
                "workflow_render_id": render.id,
                "workflow_master_id": master.id,
                "workflow_target_aspect": aspect,
                "workflow_quality_mode": quality_mode,
            },
        )
        render.task_ids = gen_ids
        task_ids.extend(gen_ids)
        bundles.append(bundle)
    multi_step.task_ids = _dedupe_nonempty([*(multi_step.task_ids or []), *task_ids])
    multi_step.input_json = {
        **(multi_step.input_json or {}),
        "active_task_ids": _dedupe_nonempty(task_ids),
    }
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=bundles)
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/renders/{render_id}/revise",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def revise_poster_render(
    workflow_run_id: str,
    render_id: str,
    body: PosterReviseIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    """单张返修：scope=background/style 走整张 i2i；scope=inpaint 走 mask inpaint。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await _sync_poster_workflow_outputs(db, run)
    render = (
        await db.execute(
            select(PosterRender).where(
                PosterRender.id == render_id,
                PosterRender.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if render is None:
        raise _http("not_found", "poster render not found", 404)
    if not render.image_id:
        raise _http("render_no_image", "render has no image yet", 409)
    if body.scope == "inpaint":
        # 走 inpaint 子端点同一逻辑；要求 mask
        return await _do_poster_inpaint(
            db,
            user=user,
            run=run,
            render=render,
            instruction=body.instruction,
            mask_image_id=body.mask_image_id or "",
        )
    master = await _poster_selected_master(db, run.id)
    style_summary = (run.metadata_jsonb or {}).get("style_summary") or {}
    copy_step = await _step(db, run.id, "copy_analysis")
    copy_analysis = copy_step.output_json or {}
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    # 参考图：母版 + 当前 render 图（让模型保持版式）
    ref_ids = _dedupe_nonempty([master.image_id or "", render.image_id])
    image_params = _poster_image_params(
        aspect_ratio=render.aspect_ratio,
        quality_mode=str(render.metadata_jsonb.get("quality_mode") or run.quality_mode),
        count=1,
    )
    bundle, _, gen_ids = await _create_poster_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.IMAGE_TO_IMAGE,
        text=_poster_revision_prompt(
            style_summary=style_summary,
            copy_analysis=copy_analysis,
            target_aspect=render.aspect_ratio,
            instruction=body.instruction,
            scope=body.scope,
        ),
        attachment_ids=ref_ids,
        idempotency_key=f"wf:{run.id[:18]}:rv:{render.id[:8]}:{new_uuid7()[:8]}",
        workflow_run_id=run.id,
        workflow_step_key="multi_size_generation",
        image_params=image_params,
        workflow_meta={
            "workflow_action": "poster_revise",
            "workflow_render_id": render.id,
            "workflow_master_id": master.id,
            "workflow_revision_scope": body.scope,
            "workflow_revision_source_image_id": render.image_id,
        },
    )
    render.task_ids = [*(render.task_ids or []), *gen_ids]
    render.status = "revising"
    multi_step = await _step(db, run.id, "multi_size_generation")
    multi_step.task_ids = _dedupe_nonempty([*(multi_step.task_ids or []), *gen_ids])
    multi_step.input_json = {
        **(multi_step.input_json or {}),
        "expected_render_count": 1,
        "active_render_id": render.id,
        "active_task_ids": _dedupe_nonempty(gen_ids),
    }
    if multi_step.status not in {"running"}:
        multi_step.status = "running"
    run.current_step = "multi_size_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


async def _do_poster_inpaint(
    db: AsyncSession,
    *,
    user: User,
    run: WorkflowRun,
    render: PosterRender,
    instruction: str,
    mask_image_id: str,
) -> WorkflowRunOut:
    """执行 inpaint：mask + 用户编辑意图 → mask_image_id 透传给 worker，
    worker 侧用 _wrap_inpaint_prompt 包裹（OpenAI invariant 模板，2026-05-07 实测）。"""
    if not mask_image_id:
        raise _http("missing_mask", "inpaint requires mask_image_id", 422)
    # mask 校验：和 render 同一用户
    await _validate_owned_images(
        db,
        user_id=user.id,
        image_ids=[mask_image_id],
        min_count=1,
        max_count=1,
    )
    conv = await _get_owned_conversation(
        db, user_id=user.id, conversation_id=run.conversation_id or ""
    )
    # 参考图：当前 render 图作为底图（mask 应用于其上）
    ref_ids = [render.image_id] if render.image_id else []
    quality_mode = str(render.metadata_jsonb.get("quality_mode") or run.quality_mode)
    image_params = _poster_image_params(
        aspect_ratio=render.aspect_ratio,
        quality_mode=quality_mode,
        count=1,
    )
    # prompt：只传用户原始编辑意图（短句），worker 侧会用 invariant 模板包装。
    bundle, _, gen_ids = await _create_poster_workflow_task(
        db=db,
        user=user,
        conv=conv,
        intent=Intent.IMAGE_TO_IMAGE,
        text=instruction.strip(),
        attachment_ids=ref_ids,
        idempotency_key=f"wf:{run.id[:18]}:in:{render.id[:8]}:{new_uuid7()[:8]}",
        workflow_run_id=run.id,
        workflow_step_key="multi_size_generation",
        image_params=image_params,
        workflow_meta={
            "workflow_action": "poster_inpaint",
            "workflow_render_id": render.id,
            "workflow_revision_source_image_id": render.image_id,
            "workflow_inpaint_mask_image_id": mask_image_id,
        },
        mask_image_id=mask_image_id,
    )
    render.task_ids = [*(render.task_ids or []), *gen_ids]
    render.status = "revising"
    multi_step = await _step(db, run.id, "multi_size_generation")
    multi_step.task_ids = _dedupe_nonempty([*(multi_step.task_ids or []), *gen_ids])
    multi_step.input_json = {
        **(multi_step.input_json or {}),
        "expected_render_count": 1,
        "active_render_id": render.id,
        "active_task_ids": _dedupe_nonempty(gen_ids),
    }
    if multi_step.status not in {"running"}:
        multi_step.status = "running"
    run.current_step = "multi_size_generation"
    run.status = "running"
    conv.last_activity_at = _now()
    await db.commit()
    await _publish_bundles(db, user_id=user.id, conv_id=conv.id, bundles=[bundle])
    run = await _get_run(db, user_id=user.id, run_id=run.id)
    out = await _build_run_out(db, run)
    await db.commit()
    return out


@router.post(
    "/{workflow_run_id}/renders/{render_id}/inpaint",
    response_model=WorkflowRunOut,
    dependencies=[Depends(verify_csrf)],
)
async def inpaint_poster_render(
    workflow_run_id: str,
    render_id: str,
    body: PosterInpaintIn,
    user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WorkflowRunOut:
    """局部 inpaint 单独端点；语义等价于 revise(scope="inpaint")，但 mask 必填。"""
    run = await _get_run(db, user_id=user.id, run_id=workflow_run_id, lock=True)
    if run.type != POSTER_WORKFLOW_TYPE:
        raise _http("wrong_workflow_type", "endpoint only valid for poster_design", 409)
    await _sync_poster_workflow_outputs(db, run)
    render = (
        await db.execute(
            select(PosterRender).where(
                PosterRender.id == render_id,
                PosterRender.workflow_run_id == run.id,
            )
        )
    ).scalar_one_or_none()
    if render is None:
        raise _http("not_found", "poster render not found", 404)
    if not render.image_id:
        raise _http("render_no_image", "render has no image yet", 409)
    return await _do_poster_inpaint(
        db,
        user=user,
        run=run,
        render=render,
        instruction=body.instruction,
        mask_image_id=body.mask_image_id,
    )
