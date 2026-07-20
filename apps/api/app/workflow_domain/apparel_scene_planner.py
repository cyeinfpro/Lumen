"""GPT-5.5 preflight planning for apparel showcase generation.

This module intentionally keeps the GPT-facing director/composer/reviewer
contract separate from ``workflows.py``. The workflow route owns persistence and
task creation; this module owns structured scene planning and safe fallbacks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import MAX_PROMPT_CHARS
from lumen_core.providers import (
    DEFAULT_LEGACY_PROVIDER_BASE_URL,
    ProviderDefinition,
    build_effective_provider_config,
    endpoint_kind_allowed,
    resolve_provider_proxy_url,
    weighted_priority_order,
)
from lumen_core.runtime_settings import get_spec
from lumen_core.vision_tagging import extract_response_text, responses_url

from ..runtime_settings import get_setting
from .apparel_scene_fallbacks import (
    _dict_or_empty,
    _is_generic_scene_text,
    _product_visibility_for_shot,
    build_garment_lock,
    clean_text,
    compact_product_context_for_gpt55,
    coerce_bool,
    coerce_string_list,
    fallback_scene_cards_from_pool,
    scene_fingerprint,
)
from .apparel_scene_fallbacks import *  # noqa: F403,F401

# Keep the historical logger name so existing operational filters and tests
# continue to observe the compatibility facade.
logger = logging.getLogger("app.routes._apparel_scene_planner")

SceneStrategy = Literal["balanced", "natural_series", "editorial_campaign"]
SceneVariety = Literal["safe", "rich", "wild"]
ScenePlannerMode = Literal["gpt55_preflight", "gpt55_batch_only", "rules_fallback"]
ContinuityAnchor = Literal["none", "accessory", "pet", "location_series"]

_PROVIDER_RR_COUNTERS: dict[int, int] = {}
_PROVIDER_RR_LOCK = asyncio.Lock()
_DIRECTOR_MODEL = "gpt-5.5"
_FALLBACK_MODEL = "gpt-5.4"
_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_GPT55_PROVIDER_LIMIT_ENV = "LUMEN_SHOWCASE_GPT_PROVIDER_LIMIT"
_GPT55_CALL_TIMEOUT_ENV = "LUMEN_SHOWCASE_GPT_CALL_TIMEOUT_SEC"
_GPT55_DEFAULT_PROVIDER_LIMIT = 2
_GPT55_DIRECTOR_TIMEOUT_SEC = 150.0
_GPT55_COMPOSER_TIMEOUT_SEC = 75.0
_GPT55_REVIEW_TIMEOUT_SEC = 45.0
_GPT55_DEFAULT_TIMEOUT_SEC = 75.0
_GPT55_ATTEMPT_TIMEOUT_SEC = 70.0
_GPT55_DIRECTOR_RETRY_ENV = "LUMEN_SHOWCASE_GPT_DIRECTOR_RETRIES"
_GPT55_DIRECTOR_DEFAULT_RETRIES = 1
_REFERENCE_IMAGE_RETRY_STATUS = {400, 413, 415, 422}
_REFERENCE_IMAGE_RETRY_TOKENS = (
    "input_image",
    "image_url",
    "data url",
    "data_url",
    "base64",
    "image too large",
    "too large",
    "invalid image",
    "invalid_image",
    "unsupported image",
    "unsupported_file",
    "content part",
    "invalid request",
)


async def plan_scene_cards_with_gpt55(
    db: AsyncSession,
    *,
    product_analysis: dict[str, Any],
    garment_lock: dict[str, Any],
    model_summary: str,
    template: str,
    scene_environment: str,
    shot_picks: list[tuple[str, dict[str, Any]]],
    aspect_ratio: str,
    output_count: int,
    user_prompt: str,
    accessory_plan: dict[str, Any],
    scene_strategy: str,
    scene_variety: str,
    continuity_anchor: str,
    allow_pet: bool,
    allow_background_people: bool,
    provider_order: list[ProviderDefinition] | None = None,
    reference_images: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    payload = {
        "product": compact_product_context_for_gpt55(product_analysis, garment_lock),
        "model": {"summary": model_summary},
        "request": {
            "count": output_count,
            "template": template,
            "scene_environment": scene_environment,
            "aspect_ratio": aspect_ratio,
            "strategy": scene_strategy,
            "variety": scene_variety,
            "continuity_anchor": continuity_anchor,
            "allow_pet": allow_pet,
            "allow_background_people": allow_background_people,
            "user_direction": user_prompt,
            "creativity_mode": (
                "bold_distinctive"
                if scene_variety == "wild"
                else "safe_controlled"
                if scene_variety == "safe"
                else "rich_varied"
            ),
            "front_view_policy": (
                "默认正面或三分之二正面；只有 shot_class=side_or_back "
                "才允许侧背或背面作为主视角。"
            ),
        },
        "shot_plan": [
            {
                "shot_class": shot_class,
                "variant_label": clean_text(variant.get("label"), max_len=140),
                "framing": variant.get("framing"),
            }
            for shot_class, variant in shot_picks
        ],
        "fallback_guardrails": {
            "do_not_copy": "不要照抄模板 shot label；你需要重新导演每张图的真实地点、事件、动作和机位。",
            "safe_if_needed": "如果上游失败，本地规则才会兜底；正常情况下以你的单张拍摄方案为准。",
        },
    }
    instructions = _director_instructions(output_count)
    retry_errors: list[str] = []
    retry_rounds = 1 + _gpt55_director_retry_count()
    last_error = ""
    for round_index in range(retry_rounds):
        try:
            raw = await _call_gpt55_json(
                db,
                purpose="apparel_scene_director",
                instructions=_director_retry_instructions(
                    instructions,
                    round_index=round_index,
                    last_error=last_error,
                ),
                payload=_director_retry_payload(
                    payload,
                    round_index=round_index,
                    last_error=last_error,
                ),
                max_output_tokens=5200 if output_count <= 8 else 9000,
                provider_order=provider_order,
                reference_images=reference_images,
            )
            cards = _normalize_scene_cards(raw.get("scene_cards"), shot_picks)
            if len(cards) != output_count:
                raise ValueError("scene card count mismatch")
            fingerprints = _unique_fingerprints(cards)
            return {
                "planner": "gpt55_preflight",
                "planner_status": "ok",
                "series_concept": clean_text(raw.get("series_concept"), max_len=160)
                or "自然服饰展示拍摄",
                "continuity_anchors": coerce_string_list(
                    raw.get("continuity_anchors"), max_items=6
                ),
                "scene_cards": cards,
                "scene_fingerprints": fingerprints,
                "risk_notes": coerce_string_list(raw.get("risk_notes"), max_items=8),
                "reference_image_fallback_reason": clean_text(
                    raw.get("reference_image_fallback_reason"), max_len=300
                )
                or None,
                "fallback_reason": None,
                "director_attempts_made": round_index + 1,
                "director_retry_count": len(retry_errors),
                "director_retry_errors": retry_errors,
            }
        except Exception as exc:  # noqa: BLE001
            last_error = clean_text(str(exc), max_len=500) or exc.__class__.__name__
            retry_errors.append(last_error)
            if round_index + 1 < retry_rounds:
                logger.warning(
                    "apparel scene director retry %s/%s after failure: %s",
                    round_index + 2,
                    retry_rounds,
                    last_error,
                )
                continue
            logger.warning(
                "apparel scene director fallback after %s rounds: %s",
                retry_rounds,
                last_error,
            )
    fallback_cards = fallback_scene_cards_from_pool(
        product_analysis=product_analysis,
        template=template,
        scene_environment=scene_environment,
        shot_picks=shot_picks,
        aspect_ratio=aspect_ratio,
        user_prompt=user_prompt,
        accessory_plan=accessory_plan,
        allow_pet=allow_pet,
        continuity_anchor=continuity_anchor,
        scene_strategy=scene_strategy,
        scene_variety=scene_variety,
    )
    fallback = _fallback_planning_result(
        fallback_cards,
        reason=f"gpt55_director_retry_exhausted: {last_error}",
    )
    fallback["director_attempts_made"] = len(retry_errors)
    fallback["director_retry_count"] = len(retry_errors)
    fallback["director_retry_errors"] = retry_errors
    return fallback


def rules_fallback_planning(
    *,
    product_analysis: dict[str, Any],
    template: str,
    scene_environment: str,
    shot_picks: list[tuple[str, dict[str, Any]]],
    aspect_ratio: str,
    user_prompt: str,
    accessory_plan: dict[str, Any],
    allow_pet: bool,
    continuity_anchor: str,
    scene_strategy: str = "natural_series",
    scene_variety: str = "rich",
) -> dict[str, Any]:
    cards = fallback_scene_cards_from_pool(
        product_analysis=product_analysis,
        template=template,
        scene_environment=scene_environment,
        shot_picks=shot_picks,
        aspect_ratio=aspect_ratio,
        user_prompt=user_prompt,
        accessory_plan=accessory_plan,
        allow_pet=allow_pet,
        continuity_anchor=continuity_anchor,
        scene_strategy=scene_strategy,
        scene_variety=scene_variety,
    )
    return _fallback_planning_result(cards, reason="rules_fallback_requested")


def _fallback_planning_result(
    cards: list[dict[str, Any]], *, reason: str
) -> dict[str, Any]:
    return {
        "planner": "rules_fallback",
        "planner_status": "fallback",
        "series_concept": "规则兜底自然服饰展示",
        "continuity_anchors": [],
        "scene_cards": cards,
        "scene_fingerprints": _unique_fingerprints(cards),
        "risk_notes": [reason[:200]] if reason else [],
        "fallback_reason": reason[:500] if reason else None,
    }


def _gpt55_director_retry_count() -> int:
    raw_retries = os.environ.get(_GPT55_DIRECTOR_RETRY_ENV)
    if raw_retries:
        try:
            return max(0, min(5, int(raw_retries)))
        except (TypeError, ValueError):
            logger.warning(
                "invalid %s=%r; using default",
                _GPT55_DIRECTOR_RETRY_ENV,
                raw_retries,
            )
    return _GPT55_DIRECTOR_DEFAULT_RETRIES


def _director_retry_payload(
    payload: dict[str, Any],
    *,
    round_index: int,
    last_error: str,
) -> dict[str, Any]:
    if round_index <= 0:
        return payload
    failure_summary = _director_retry_failure_summary(last_error)
    return {
        **payload,
        "retry_context": {
            "attempt": round_index + 1,
            "previous_failure": failure_summary,
            "correction_required": (
                "修正上一轮失败点，重新完整输出整批 scene_cards。"
                "不要省字段、不要用泛化动作、不要重复地点/动作/指纹，"
                "非 side_or_back 镜头不得写背影或纯侧面；"
                "wild/bold 模式仍要保留独特视觉钩子。"
            ),
        },
    }


def _director_retry_instructions(
    instructions: str,
    *,
    round_index: int,
    last_error: str,
) -> str:
    if round_index <= 0:
        return instructions
    error = _director_retry_failure_summary(last_error)
    return (
        f"{instructions}\n\n"
        f"【重试修正】这是第 {round_index + 1} 轮导演请求。上一轮失败原因：{error}。"
        "这一次必须针对失败原因完整修正并重新输出整批 JSON，不要只输出补丁。"
        "所有 required fields 都要具体可拍摄；micro_event、pose、motion 不能写成"
        "自然站姿、正面全身、商品展示这类泛化词；不得重复地点、动作或构图；"
        "除 shot_class=side_or_back 外，不得把主视角写成背影、背向、后背或纯侧面。"
    )


def _director_retry_failure_summary(error: str) -> str:
    text = str(error or "").strip().lower()
    if not text:
        return "上一轮输出未通过校验，请重新输出完整 JSON。"
    if "incomplete gpt scene_card" in text:
        return "上一轮有 scene_card 字段不完整，请补齐所有必填字段和 camera 子字段。"
    if "missing gpt scene_card" in text or "scene card count mismatch" in text:
        return "上一轮 scene_cards 数量不完整，请严格按 shot_plan 输出每一张。"
    if "generic gpt micro_event" in text:
        return "上一轮 micro_event 太泛，请改成具体生活事件。"
    if "generic gpt pose" in text:
        return "上一轮 pose 太泛，请改成具体身体朝向、重心和手部位置。"
    if "generic gpt motion" in text:
        return "上一轮 motion 太泛，请改成具体可见动态。"
    if "back/side view" in text:
        return "上一轮非侧背镜头使用了背面或纯侧面主视角，请改为正面或三分之二正面。"
    if "duplicate gpt scene fingerprint" in text:
        return "上一轮有重复场景或动作，请让每张地点、事件、机位和构图明显不同。"
    if "json" in text:
        return "上一轮 JSON 无法解析或结构不正确，请只输出完整 JSON 对象。"
    if "timeout" in text or "timed out" in text or "exceeded" in text:
        return "上一轮上游调用超时，请更简洁地输出完整 JSON。"
    if "http" in text or "upstream" in text or "provider" in text:
        return "上一轮上游模型调用失败，请重新输出完整 JSON。"
    return "上一轮输出未通过校验，请重新输出完整 JSON。"


def _director_instructions(output_count: int) -> str:
    return (
        "你是服饰电商真人模特图的拍摄导演兼提示词摄影师。你要一次性为整批图片生成"
        "自然、不重复、像真实拍摄分镜的单张拍摄方案，并给每张写一条可直接拼接到"
        "GPT Image 2 生图 prompt 的短摄影提示词 shooting_brief。场景、姿势、微动作、"
        "镜头和光线全部由你决定，不要照抄 shot_plan 的标签或 fallback 文案。"
        "必须只输出 JSON 对象，不要 Markdown。\n"
        "如果输入里带有参考图，参考图会标注为商品图和已确认模特图；你必须直接观察"
        "服饰风格、模特年龄感、身材比例、发型气质和二者搭配关系，再设计更适合这组搭配的"
        "电商宣传照场景、动作、神态、构图和光线。不要描述或复述衣服细节，"
        "商品还原约束会由系统后续拼接。\n"
        "目标是摄影大师级的商业环境肖像：要有张力、活力、动态感和超真实摄影质感，"
        "但不引用具体摄影师、品牌或杂志名。\n"
        "活力不是大幅夸张摆拍，而是清楚的中等动态瞬间：走近、起步、落步、半转、"
        "回头、轻快跨步、衣摆摆动、发丝轻动、回应镜头外的人。front_full_body 和 "
        "natural_pose 不能退成静态站姿；除非商品极易被遮挡，否则每张都要有可见的"
        "身体重心变化或脚步方向。detail_half_body 也要有手指、肩颈或眼神的动作半拍。"
        "任何动态都必须让手、头发、道具避开胸前、图案、口袋和商品主体。\n"
        f"scene_cards 必须正好 {output_count} 条，且第 i 条必须严格对应 "
        "shot_plan[i]，id 用 shot_plan[i].shot_class 加 '-' 加索引，例如 "
        "detail_half_body-3。禁止重排 shot_plan 顺序。\n"
        "默认视角偏正面：除 shot_class 为 side_or_back 的少量补充图外，"
        "scene_card 必须是正面或三分之二正面，脸部和商品正面主体清楚。"
        "不要把 front_full_body、natural_pose、detail_half_body 写成背影、"
        "背向前走、后背主视角或纯侧面轮廓。\n"
        "字段：series_concept, continuity_anchors, scene_cards, risk_notes。\n"
        "每个 scene_card 字段必须有 id, scene_family, location, micro_event, camera, "
        "pose, motion, props, lighting, composition, product_visibility, "
        "environment_detail, lighting_detail, camera_detail, composition_detail, "
        "creative_intent, natural_detail, shooting_brief, negative。\n"
        "camera 必须有 distance, angle, lens_feel, orientation。\n"
        "shooting_brief 是本张最终摄影提示词，只写场景、动作、神态、构图、光线、镜头、"
        "动态张力和真实摄影质感，120-260 字中文；不要写多个候选，不要自评打分，"
        "不要写商品清单、商品身份、禁改条款、模特一致条款或内部字段名。\n"
        "creative_intent 要写这张图的摄影作品想法，例如决定性瞬间、空间张力、"
        "光影叙事、人物与环境关系或真实生活观察；不要模仿或引用具体摄影师姓名、"
        "杂志名、品牌名。"
        "environment_detail 要写真实空间层次、背景材质、前中后景关系；"
        "lighting_detail 要写光线方向、阴影、高光和不过曝控制；"
        "camera_detail 要写镜头距离、透视、机位高度和抓拍感；"
        "composition_detail 要写主体位置、留白、裁切边界和背景不抢主体；"
        "natural_detail 要写表情、手指、身体重心、衣料受力/褶皱等自然细节。"
        "这些字段要具体到可拍摄，不要写抽象词如高级、自然、好看。"
        "product 只有少量服装关键词，只用于判断风格、年龄感和当前角度可见区域；"
        "不要输出商品身份、必须保留、禁止改色、禁改款等商品还原条款，"
        "也不要枚举商品细节。动作和道具不得遮挡商品主体。"
        "每张 micro_event 必须是具体生活事件，不能直接复制 variant_label 或写成"
        "正面全身/自然动作/自然站姿。camera angle/distance、地点、身体重心、"
        "手部动作至少两项要变化，禁止整批退回普通棚拍站姿。"
        "如果 request.variety 是 wild 或 request.creativity_mode 是 bold_distinctive，"
        "你必须显著提高独特性：每张至少有一个清楚的视觉钩子，由你基于参考图和商品气质"
        "即时构思，例如非常规但合理的地点、强图形光影、低机位、运动定格、色块留白、"
        "前后景层次或戏剧性构图。不要从模板或固定地点池选场景；不要输出普通试衣间、"
        "普通窗边、普通街角、普通棚拍站姿。大胆仍要真实、儿童合适、商品主体清楚，"
        "不能靠遮挡道具、怪异姿势或换商品来制造独特。"
        "不要使用“稳定站定展示”“只保留轻微落步感”这类会杀掉动作能量的方案；"
        "需要降低风险时，改为安全动态抓拍，例如双手低位、手臂打开、脚步刚落地、"
        "半转回头或向镜头走近。"
        "可以有连续元素，但不能让宠物、包、饮料、手机抢主体。"
        "童装/儿童必须年龄合适，不能成人化。"
    )


def _sanitize_shooting_brief(value: Any, *, max_len: int = 1800) -> str:
    text = clean_text(value, max_len=max_len)
    return (
        text.replace("SceneCard", "本张拍摄方案")
        .replace("scene_card", "拍摄方案")
        .replace("shot_plan", "拍摄计划")
        .replace("final_prompt", "拍摄方案")
    )


def _coerce_candidate_briefs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    briefs: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = (
                item.get("shooting_brief")
                or item.get("brief")
                or item.get("description")
                or item.get("text")
            )
        else:
            text = item
        brief = _sanitize_shooting_brief(text, max_len=900)
        if brief:
            briefs.append(brief)
        if len(briefs) >= 3:
            break
    return briefs


def _coerce_selection_scores(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    numeric_keys = (
        "product_visibility",
        "naturalness",
        "photographic_quality",
        "variety",
        "risk_control",
        "total",
    )
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            continue
        row: dict[str, Any] = {
            "candidate": clean_text(
                item.get("candidate") or item.get("id") or index, max_len=30
            )
            or str(index)
        }
        for key in numeric_keys:
            if key not in item:
                continue
            score = item.get(key)
            if score is None:
                row[key] = clean_text(score, max_len=20)
                continue
            try:
                row[key] = round(float(score), 2)
            except (TypeError, ValueError):
                row[key] = clean_text(score, max_len=20)
        reason = clean_text(item.get("reason"), max_len=140)
        if reason:
            row["reason"] = reason
        out.append(row)
        if len(out) >= 3:
            break
    return out


async def compose_image_prompt_with_gpt55(
    db: AsyncSession,
    *,
    base_prompt: str,
    product_analysis: dict[str, Any],
    garment_lock: dict[str, Any],
    model_summary: str,
    scene_card: dict[str, Any],
    shot_class: str,
    template: str,
    aspect_ratio: str,
    final_quality: str,
    rewrite_instruction: str | None = None,
    provider_order: list[ProviderDefinition] | None = None,
    reference_images: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    camera = _dict_or_empty(scene_card.get("camera"))
    product_context = compact_product_context_for_gpt55(product_analysis, garment_lock)
    payload = {
        "product_context": {
            **product_context,
            "current_view_visibility": clean_text(
                scene_card.get("product_visibility"), max_len=80
            ),
        },
        "model_context": clean_text(model_summary, max_len=180),
        "seed_keywords": {
            "scene_family": clean_text(scene_card.get("scene_family"), max_len=80),
            "location": clean_text(scene_card.get("location"), max_len=140),
            "micro_event": clean_text(scene_card.get("micro_event"), max_len=180),
            "camera": {
                "distance": clean_text(camera.get("distance"), max_len=60),
                "angle": clean_text(camera.get("angle"), max_len=60),
                "lens_feel": clean_text(camera.get("lens_feel"), max_len=80),
                "orientation": clean_text(camera.get("orientation"), max_len=40),
            },
            "pose": clean_text(scene_card.get("pose"), max_len=180),
            "motion": clean_text(scene_card.get("motion"), max_len=180),
            "props": coerce_string_list(
                scene_card.get("props"), max_items=5, max_len=80
            ),
            "lighting": clean_text(scene_card.get("lighting"), max_len=160),
            "composition": clean_text(scene_card.get("composition"), max_len=180),
            "environment_detail": clean_text(
                scene_card.get("environment_detail"), max_len=220
            ),
            "lighting_detail": clean_text(
                scene_card.get("lighting_detail"), max_len=220
            ),
            "camera_detail": clean_text(scene_card.get("camera_detail"), max_len=220),
            "composition_detail": clean_text(
                scene_card.get("composition_detail"), max_len=220
            ),
            "creative_intent": clean_text(
                scene_card.get("creative_intent"), max_len=220
            ),
            "natural_detail": clean_text(scene_card.get("natural_detail"), max_len=220),
            "negative": coerce_string_list(
                scene_card.get("negative"), max_items=8, max_len=100
            ),
        },
        "request": {
            "shot_class": shot_class,
            "template_hint": template,
            "aspect_ratio": aspect_ratio,
            "final_quality": final_quality,
            "system_will_append_product_lock": True,
            "candidate_count": 1,
            "view_policy": (
                "side_or_back_allowed"
                if shot_class == "side_or_back"
                else "front_or_three_quarter_required"
            ),
            "system_prompt_chars": len(base_prompt),
        },
        "rewrite_instruction": rewrite_instruction or "",
    }
    instructions = (
        "你是服饰真人图的拍摄导演，只负责把少量场景关键词扩展成单张"
        "自然摄影拍摄方案。系统稍后会把商品 1:1 还原、模特一致、禁改项"
        "和遮挡规则确定性拼接到最终生图 prompt；你不要重写这些商品约束。"
        "必须只输出 JSON 对象，不要 Markdown。\n"
        "如果输入里带有商品图和已确认模特图，先观察两张图的实际搭配关系、年龄感、"
        "体态比例和气质，再把 seed_keywords 扩展成适合 GPT Image 2 的生图摄影提示词。"
        "不要描述衣服本身，不要列商品细节。\n"
        "最终 shooting_brief 要比普通电商站姿更有创造性：更明确的瞬间、更大胆但可信的"
        "机位/光影/留白、更强的动态张力，同时保持超真实摄影和商品主体清楚。\n"
        "字段：shooting_brief, scene_keywords, composition_keywords, lighting_keywords, "
        "action_keywords, photographic_idea_keywords, product_visibility_checklist, "
        "negative_prompt_notes, regenerate_if。\n"
        "只输出 1 条最终 shooting_brief，不要先写多个候选，不要自评打分。"
        "shooting_brief 写 120-260 字中文，保持像真实生图提示词一样短而有力；"
        "product_context 只有少量服装关键词，用来判断场景气质和避免遮挡；"
        "不要把它扩写成商品清单。只写本张的场景、动作、神态、构图、光线、"
        "镜头、动态张力和真实摄影质感。"
        "必须有摄影作品感：像成熟摄影师完成的服饰纪实或环境肖像，包含一个清楚的"
        "摄影意图，例如决定性瞬间、空间张力、光影叙事、人物与环境关系、真实生活观察；"
        "不要模仿或引用具体摄影师姓名、杂志名、品牌名。"
        "语言风格参考：高级儿童时装品牌大片、真实动态抓拍、低机位儿童视角、"
        "黄昏逆光/几何阴影/大面积留白/前景虚化/高速快门/35mm/50mm/70mm 镜头等具体摄影词；"
        "不要写成规则清单，不要用模板编号，不要解释意图。"
        "除非 request.shot_class 是 side_or_back 或 seed_keywords.camera.angle 明确为 side_or_back，"
        "candidate_briefs 和 shooting_brief 必须保持正面或三分之二正面，"
        "脸部和商品主体清楚；不要写背影、背向前走、后背主视角或纯侧面轮廓。"
        "必须保留 seed_keywords 里的 location、micro_event、pose、motion、camera，"
        "creative_intent，但要把它们扩展成可直接拍摄的自然画面，不得简化成普通站姿。"
        "只用 seed_keywords 作为场景来源；不要混入其它地点、花坛、街边、棚拍、"
        "户外/室内光线，除非它们已经在 seed_keywords 里。"
        "不要输出或提到 SceneCard、scene_card、shot_plan、template、final_prompt "
        "等内部词。不要写“商品身份/必须保留/禁止改色/禁改款/模特一致”等条款，"
        "不要枚举商品所有细节；只能用“商品主体、当前角度可见的服装结构、衣料纹理”"
        "这类泛称。"
        "本张只要求当前镜头能看到的商品区域清楚；半身/上身近景不要强求背后、裙摆、"
        "全身廓形等不可见细节。不要引入新图案、logo、口袋、腰带或遮挡道具。"
        "如果有 rewrite_instruction，按它改写 shooting_brief 来降低风险；"
        "但风险改写只能移动手、头发、道具和前景位置，或降低遮挡动作幅度，"
        "不得把原本的行走、落步、半转、回头等动态改成静态站姿。"
    )
    try:
        raw = await _call_gpt55_json(
            db,
            purpose="apparel_prompt_composer",
            instructions=instructions,
            payload=payload,
            max_output_tokens=1400,
            provider_order=provider_order,
            reference_images=reference_images,
        )
        shooting_brief = _sanitize_shooting_brief(
            raw.get("shooting_brief") or raw.get("final_prompt"),
            max_len=min(1800, MAX_PROMPT_CHARS),
        )
        candidate_briefs = _coerce_candidate_briefs(raw.get("candidate_briefs"))
        if shooting_brief and shooting_brief not in candidate_briefs:
            candidate_briefs = [*candidate_briefs, shooting_brief][:3]
        if len(shooting_brief) < 60:
            raise ValueError("shooting brief too short")
        return {
            "scene_card_id": clean_text(scene_card.get("id"), max_len=80),
            "status": "ok",
            "shooting_brief": shooting_brief,
            "final_prompt": shooting_brief,
            "candidate_briefs": candidate_briefs,
            "selected_candidate_index": clean_text(
                raw.get("selected_candidate_index") or raw.get("selected_candidate"),
                max_len=20,
            )
            or None,
            "selection_scores": _coerce_selection_scores(raw.get("selection_scores")),
            "scene_keywords": coerce_string_list(
                raw.get("scene_keywords"), max_items=8, max_len=80
            ),
            "composition_keywords": coerce_string_list(
                raw.get("composition_keywords"), max_items=8, max_len=80
            ),
            "lighting_keywords": coerce_string_list(
                raw.get("lighting_keywords"), max_items=8, max_len=80
            ),
            "action_keywords": coerce_string_list(
                raw.get("action_keywords"), max_items=8, max_len=80
            ),
            "photographic_idea_keywords": coerce_string_list(
                raw.get("photographic_idea_keywords"), max_items=8, max_len=80
            ),
            "product_visibility_checklist": coerce_string_list(
                raw.get("product_visibility_checklist"), max_items=8, max_len=100
            ),
            "negative_prompt_notes": coerce_string_list(
                raw.get("negative_prompt_notes"), max_items=8, max_len=100
            ),
            "regenerate_if": coerce_string_list(
                raw.get("regenerate_if"), max_items=8, max_len=120
            ),
            "reference_image_fallback_reason": clean_text(
                raw.get("reference_image_fallback_reason"), max_len=300
            )
            or None,
            "fallback_reason": None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("apparel prompt composer fallback: %s", exc)
        return fallback_prompt_composition(
            base_prompt=base_prompt,
            scene_card=scene_card,
            reason=str(exc),
        )


def fallback_prompt_composition(
    *,
    base_prompt: str,
    scene_card: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "scene_card_id": clean_text(scene_card.get("id"), max_len=80),
        "status": "fallback",
        "shooting_brief": "",
        "final_prompt": "",
        "candidate_briefs": [],
        "selected_candidate_index": None,
        "selection_scores": [],
        "scene_keywords": [],
        "composition_keywords": [],
        "lighting_keywords": [],
        "action_keywords": [],
        "photographic_idea_keywords": [],
        "product_visibility_checklist": [],
        "negative_prompt_notes": coerce_string_list(
            scene_card.get("negative"), max_items=8, max_len=100
        ),
        "regenerate_if": [],
        "fallback_reason": reason[:500],
    }


async def review_prompt_risk_with_gpt55(
    db: AsyncSession,
    *,
    final_prompt: str,
    garment_lock: dict[str, Any],
    scene_card: dict[str, Any],
    batch_context: dict[str, Any],
    provider_order: list[ProviderDefinition] | None = None,
) -> dict[str, Any]:
    payload = {
        "final_prompt": final_prompt,
        "garment_lock": garment_lock,
        "scene_card": scene_card,
        "batch_context": batch_context,
    }
    instructions = (
        "你是服饰电商图片生成前的风险审稿员。只检查 prompt，不看图片。"
        "必须只输出 JSON 对象，不要 Markdown。字段：risk_level, risks, "
        "must_rewrite, rewrite_instruction。risk_level 只能 low/medium/high。"
        "若 prompt 可能改商品、遮挡商品主体、动作过复杂、和批次重复、或宠物/道具抢主体，"
        "必须标记风险并给出简短 rewrite_instruction。中等动态本身不是风险："
        "走近、落步、半转、回头、衣摆摆动、发丝轻动都应保留。"
        "rewrite_instruction 只能具体移动手、头发、道具、前景或调整机位以避开商品主体；"
        "禁止要求改成“稳定站定”“站定展示”“静态展示”或“只保留轻微落步感”。"
        "如果动作会遮挡，改成安全动态抓拍，例如双手低位、手臂打开、脚步刚落地、"
        "半转回头或向镜头走近，同时保持商品主体清楚。"
    )
    try:
        raw = await _call_gpt55_json(
            db,
            purpose="apparel_prompt_risk_review",
            instructions=instructions,
            payload=payload,
            max_output_tokens=900,
            provider_order=provider_order,
        )
        risk_level = clean_text(raw.get("risk_level"), max_len=20).lower()
        if risk_level not in {"low", "medium", "high"}:
            risk_level = "medium"
        risks = coerce_string_list(raw.get("risks"), max_items=8, max_len=120)
        must_rewrite = coerce_bool(raw.get("must_rewrite")) or risk_level == "high"
        return {
            "scene_card_id": clean_text(scene_card.get("id"), max_len=80),
            "status": "ok",
            "risk_level": risk_level,
            "risks": risks,
            "must_rewrite": must_rewrite,
            "rewrite_instruction": clean_text(
                raw.get("rewrite_instruction"), max_len=240
            ),
            "fallback_reason": None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("apparel risk review fallback: %s", exc)
        return fallback_risk_review(scene_card=scene_card, reason=str(exc))


def fallback_risk_review(
    *, scene_card: dict[str, Any], reason: str | None = None
) -> dict[str, Any]:
    return {
        "scene_card_id": clean_text(scene_card.get("id"), max_len=80),
        "status": "fallback",
        "risk_level": "medium",
        "risks": [reason[:200]] if reason else [],
        "must_rewrite": False,
        "rewrite_instruction": "",
        "fallback_reason": reason[:500] if reason else None,
    }


_BACK_VIEW_TEXT_TOKENS = (
    "背影",
    "背向",
    "背对",
    "背面",
    "后背",
    "背后",
    "侧后",
    "from behind",
    "back view",
    "rear view",
    "back-facing",
)
_SIDE_BACK_VIEW_TOKENS = (
    *_BACK_VIEW_TEXT_TOKENS,
    "side_or_back",
    "side view",
    "profile view",
    "pure side",
    "纯侧面",
    "侧面轮廓",
    "侧背",
)
_SIDE_BACK_CAMERA_ANGLE_TOKENS = (
    *_SIDE_BACK_VIEW_TOKENS,
    "side profile",
    "side-profile",
    "side_profile",
    "profile",
    "side",
    "back",
    "rear",
    "behind",
)
_FRONT_CAMERA_ANGLE_TOKENS = (
    "front",
    "front view",
    "front_view",
    "front three quarter",
    "front-three-quarter",
    "front_three_quarter",
    "three quarter front",
    "three-quarter-front",
    "three_quarter_front",
    "3/4 front",
    "eye level",
    "eye-level",
    "eye_level",
    "straight on",
    "straight-on",
    "straight_on",
    "正面",
    "三分之二正面",
    "四分之三正面",
    "平视",
)


def _has_view_token(value: Any, tokens: tuple[str, ...]) -> bool:
    text = str(value or "").lower()
    return any(token.lower() in text for token in tokens)


def _camera_angle_has_token(value: Any, tokens: tuple[str, ...]) -> bool:
    raw = str(value or "").strip().lower()
    if not raw:
        return False
    normalized = re.sub(r"[_-]+", " ", raw)
    for token in tokens:
        token_text = str(token or "").strip().lower()
        if not token_text:
            continue
        token_normalized = re.sub(r"[_-]+", " ", token_text)
        if re.search(
            rf"(?<![a-z0-9]){re.escape(token_normalized)}(?![a-z0-9])",
            normalized,
        ):
            return True
        if any("\u4e00" <= ch <= "\u9fff" for ch in token_text) and token_text in raw:
            return True
    return False


def _required_gpt_scene_fields_missing(card: dict[str, Any]) -> list[str]:
    required = (
        "location",
        "micro_event",
        "pose",
        "motion",
        "lighting",
        "composition",
        "environment_detail",
        "lighting_detail",
        "camera_detail",
        "composition_detail",
        "creative_intent",
        "natural_detail",
        "shooting_brief",
    )
    missing = [key for key in required if not str(card.get(key) or "").strip()]
    camera = _dict_or_empty(card.get("camera"))
    for key in ("distance", "angle", "lens_feel", "orientation"):
        if not str(camera.get(key) or "").strip():
            missing.append(f"camera.{key}")
    return missing


def _reject_side_back_for_non_side_card(card: dict[str, Any], shot_class: str) -> None:
    camera = _dict_or_empty(card.get("camera"))
    camera_angle = camera.get("angle")
    if shot_class == "side_or_back":
        if not (
            _has_view_token(camera_angle, _SIDE_BACK_VIEW_TOKENS)
            or _camera_angle_has_token(camera_angle, _SIDE_BACK_CAMERA_ANGLE_TOKENS)
        ):
            raise ValueError(
                "side_or_back GPT scene_card must use side/back camera angle"
            )
        return
    if _camera_angle_has_token(camera_angle, _SIDE_BACK_CAMERA_ANGLE_TOKENS):
        raise ValueError("non-side GPT scene_card uses back/side view camera angle")
    if shot_class in {
        "front_full_body",
        "natural_pose",
    } and not _camera_angle_has_token(
        camera_angle,
        _FRONT_CAMERA_ANGLE_TOKENS,
    ):
        raise ValueError(
            f"{shot_class} GPT scene_card camera.angle must stay front-facing"
        )
    checked = {
        "camera.angle": camera_angle,
        "product_visibility": card.get("product_visibility"),
        "micro_event": card.get("micro_event"),
        "pose": card.get("pose"),
        "motion": card.get("motion"),
        "composition": card.get("composition"),
        "camera_detail": card.get("camera_detail"),
        "composition_detail": card.get("composition_detail"),
        "creative_intent": card.get("creative_intent"),
        "natural_detail": card.get("natural_detail"),
        "shooting_brief": card.get("shooting_brief"),
    }
    offenders = [
        key
        for key, value in checked.items()
        if _has_view_token(value, _SIDE_BACK_VIEW_TOKENS)
    ]
    if offenders:
        raise ValueError(
            f"non-side GPT scene_card uses back/side view in {', '.join(offenders)}"
        )


def _scene_card_match_index(
    raw: dict[str, Any],
    shot_picks: list[tuple[str, dict[str, Any]]],
    taken: list[bool],
) -> int | None:
    raw_id = clean_text(raw.get("id"), max_len=100).lower()
    exact_matches = [
        index
        for index, (shot_class, _variant) in enumerate(shot_picks)
        if not taken[index] and raw_id == f"{shot_class.lower()}-{index + 1}"
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]

    class_matches = [
        index
        for index, (shot_class, _variant) in enumerate(shot_picks)
        if not taken[index] and shot_class.lower() in raw_id
    ]
    if len(class_matches) == 1:
        return class_matches[0]

    visibility = clean_text(raw.get("product_visibility"), max_len=80)
    visibility_matches = [
        index
        for index, (shot_class, _variant) in enumerate(shot_picks)
        if (
            not taken[index]
            and visibility
            and visibility == _product_visibility_for_shot(shot_class)
        )
    ]
    return visibility_matches[0] if len(visibility_matches) == 1 else None


def _align_scene_cards(
    raw_cards: Any,
    shot_picks: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any] | None]:
    cards = raw_cards if isinstance(raw_cards, list) else []
    aligned: list[dict[str, Any] | None] = [None] * len(shot_picks)
    taken = [False] * len(shot_picks)
    leftover: list[dict[str, Any]] = []
    for raw in cards:
        if not isinstance(raw, dict):
            continue
        matched_index = _scene_card_match_index(raw, shot_picks, taken)
        if matched_index is None:
            leftover.append(raw)
            continue
        aligned[matched_index] = raw
        taken[matched_index] = True
    for index, card in enumerate(aligned):
        if card is None and leftover:
            aligned[index] = leftover.pop(0)
    return aligned


def _validate_normalized_scene_card(
    card: dict[str, Any],
    *,
    index: int,
    shot_class: str,
    shot_label: str,
) -> dict[str, Any]:
    missing = _required_gpt_scene_fields_missing(card)
    if missing:
        raise ValueError(
            f"incomplete GPT scene_card for shot {index + 1}: {', '.join(missing)}"
        )
    for field, label in (
        ("micro_event", "micro_event"),
        ("pose", "pose"),
        ("motion", "motion"),
    ):
        if _is_generic_scene_text(
            card.get(field),
            shot_class=shot_class,
            label=shot_label,
        ):
            raise ValueError(
                f"generic GPT {label} for shot {index + 1}: {card.get(field)}"
            )
    _reject_side_back_for_non_side_card(card, shot_class)
    card["fingerprint"] = scene_fingerprint(card)
    return card


def _normalize_scene_card(
    raw: dict[str, Any] | None,
    *,
    index: int,
    shot_picks: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"missing GPT scene_card for shot {index + 1}")
    shot_class = shot_picks[index][0]
    shot_label = clean_text(shot_picks[index][1].get("label"), max_len=160)
    camera = _dict_or_empty(raw.get("camera"))
    card = {
        "id": clean_text(raw.get("id"), max_len=80) or f"scene-{index + 1:02d}",
        "scene_family": clean_text(raw.get("scene_family"), max_len=60)
        or "gpt55_scene",
        "location": clean_text(raw.get("location"), max_len=120),
        "micro_event": clean_text(raw.get("micro_event"), max_len=160),
        "camera": {
            "distance": clean_text(camera.get("distance"), max_len=40),
            "angle": clean_text(camera.get("angle"), max_len=40),
            "lens_feel": clean_text(camera.get("lens_feel"), max_len=60),
            "orientation": clean_text(camera.get("orientation"), max_len=40),
        },
        "pose": clean_text(raw.get("pose"), max_len=160),
        "motion": clean_text(raw.get("motion"), max_len=160),
        "props": coerce_string_list(raw.get("props"), max_items=6, max_len=50),
        "lighting": clean_text(raw.get("lighting"), max_len=120),
        "composition": clean_text(raw.get("composition"), max_len=180),
        "product_visibility": clean_text(raw.get("product_visibility"), max_len=80)
        or _product_visibility_for_shot(shot_class),
        "environment_detail": clean_text(raw.get("environment_detail"), max_len=220),
        "lighting_detail": clean_text(raw.get("lighting_detail"), max_len=220),
        "camera_detail": clean_text(raw.get("camera_detail"), max_len=220),
        "composition_detail": clean_text(raw.get("composition_detail"), max_len=220),
        "creative_intent": clean_text(raw.get("creative_intent"), max_len=220),
        "natural_detail": clean_text(raw.get("natural_detail"), max_len=220),
        "shooting_brief": _sanitize_shooting_brief(
            raw.get("shooting_brief") or raw.get("final_prompt"),
            max_len=900,
        ),
        "negative": coerce_string_list(raw.get("negative"), max_items=8, max_len=100),
        "source": "gpt55",
    }
    return _validate_normalized_scene_card(
        card,
        index=index,
        shot_class=shot_class,
        shot_label=shot_label,
    )


def _normalize_scene_cards(
    raw_cards: Any,
    shot_picks: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    aligned = _align_scene_cards(raw_cards, shot_picks)
    normalized = [
        _normalize_scene_card(raw, index=index, shot_picks=shot_picks)
        for index, raw in enumerate(aligned)
    ]
    return _assert_unique_scene_fingerprints(normalized)


def _assert_unique_scene_fingerprints(
    cards: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, card in enumerate(cards):
        fingerprint = scene_fingerprint(card)
        if fingerprint in seen:
            raise ValueError(f"duplicate GPT scene fingerprint at shot {index + 1}")
        seen.add(fingerprint)
        card["fingerprint"] = fingerprint
        out.append(card)
    return out


def _unique_fingerprints(cards: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for card in cards:
        fingerprint = scene_fingerprint(card)
        if fingerprint and fingerprint not in seen:
            seen.add(fingerprint)
            out.append(fingerprint)
    return out


async def resolve_scene_provider_order(db: AsyncSession) -> list[ProviderDefinition]:
    spec_providers = get_spec("providers")
    raw_providers = await get_setting(db, spec_providers) if spec_providers else None
    providers, _proxies, errors = build_effective_provider_config(
        raw_providers=raw_providers,
        legacy_base_url=(
            os.environ.get("UPSTREAM_BASE_URL") or DEFAULT_LEGACY_PROVIDER_BASE_URL
        ),
        legacy_api_key=os.environ.get("UPSTREAM_API_KEY"),
    )
    for err in errors:
        logger.warning("%s", err)
    providers = [p for p in providers if endpoint_kind_allowed(p, "responses")]
    async with _PROVIDER_RR_LOCK:
        return weighted_priority_order(providers, _PROVIDER_RR_COUNTERS)


async def _call_gpt55_json(
    db: AsyncSession,
    *,
    purpose: str,
    instructions: str,
    payload: dict[str, Any],
    max_output_tokens: int,
    provider_order: list[ProviderDefinition] | None = None,
    reference_images: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    providers = (
        list(provider_order)
        if provider_order is not None
        else await resolve_scene_provider_order(db)
    )
    providers = _limit_gpt55_providers(providers)
    if not providers:
        raise RuntimeError("no responses provider available")
    primary_effort = "medium" if purpose == "apparel_scene_director" else "low"
    attempts = (
        {
            "name": "gpt55-priority",
            "model": _DIRECTOR_MODEL,
            "reasoning": {"effort": primary_effort},
            "service_tier": "priority",
        },
        {
            "name": "gpt55-standard",
            "model": _DIRECTOR_MODEL,
            "reasoning": {"effort": "low"},
            "service_tier": None,
        },
        {
            "name": "gpt54-standard-fallback",
            "model": _FALLBACK_MODEL,
            "reasoning": {"effort": "low"},
            "service_tier": None,
        },
    )
    last_error = "unknown"
    call_timeout = _gpt55_call_timeout_seconds(purpose)
    deadline = asyncio.get_running_loop().time() + call_timeout
    for provider in providers:
        provider_fatal = False
        reference_image_fallback_reason: str | None = None
        for attempt in attempts:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise RuntimeError(
                    f"{purpose} exceeded {call_timeout:g}s GPT JSON budget; "
                    f"last_error={last_error}"
                )
            attempt_timeout = min(_GPT55_ATTEMPT_TIMEOUT_SEC, remaining)
            attempt_reference_images = (
                None if reference_image_fallback_reason else reference_images
            )
            try:
                text = await _call_responses_text_with_timeout(
                    provider=provider,
                    attempt=attempt,
                    purpose=purpose,
                    instructions=instructions,
                    payload=payload,
                    max_output_tokens=max_output_tokens,
                    reference_images=attempt_reference_images,
                    timeout_seconds=attempt_timeout,
                )
                data = _extract_json_object(text)
                if isinstance(data, dict):
                    if reference_image_fallback_reason:
                        data.setdefault(
                            "reference_image_fallback_reason",
                            reference_image_fallback_reason[:300],
                        )
                    return data
                raise ValueError("json root is not object")
            except Exception as exc:  # noqa: BLE001
                last_error = f"{provider.name}/{attempt['name']}: {exc}"
                logger.info("gpt55 json attempt failed: %s", last_error)
                decision_exc = exc
                if attempt_reference_images and _should_retry_without_reference_images(
                    exc
                ):
                    reference_image_fallback_reason = last_error[:300]
                    try:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise _Gpt55CallTimeout(
                                f"timed out after {call_timeout:g}s total budget"
                            )
                        logger.info(
                            "gpt55 json retrying without reference images: %s",
                            last_error,
                        )
                        text = await _call_responses_text_with_timeout(
                            provider=provider,
                            attempt=attempt,
                            purpose=purpose,
                            instructions=instructions,
                            payload=payload,
                            max_output_tokens=max_output_tokens,
                            reference_images=None,
                            timeout_seconds=min(
                                _GPT55_ATTEMPT_TIMEOUT_SEC,
                                remaining,
                            ),
                        )
                        data = _extract_json_object(text)
                        if isinstance(data, dict):
                            data.setdefault(
                                "reference_image_fallback_reason",
                                reference_image_fallback_reason,
                            )
                            return data
                        raise ValueError("json root is not object")
                    except Exception as text_exc:  # noqa: BLE001
                        last_error = (
                            f"{provider.name}/{attempt['name']} text-only: {text_exc}"
                        )
                        logger.info("gpt55 text-only retry failed: %s", last_error)
                        decision_exc = text_exc
                if not _should_try_next_attempt(decision_exc):
                    provider_fatal = True
                    break
        if provider_fatal:
            continue
    raise RuntimeError(last_error)


class _Gpt55CallTimeout(TimeoutError):
    pass


def _gpt55_provider_limit() -> int:
    raw_limit = os.environ.get(_GPT55_PROVIDER_LIMIT_ENV)
    if raw_limit:
        try:
            return max(1, min(16, int(raw_limit)))
        except (TypeError, ValueError):
            logger.warning(
                "invalid %s=%r; using default",
                _GPT55_PROVIDER_LIMIT_ENV,
                raw_limit,
            )
    return _GPT55_DEFAULT_PROVIDER_LIMIT


def _limit_gpt55_providers(
    providers: list[ProviderDefinition],
) -> list[ProviderDefinition]:
    if not providers:
        return providers
    return providers[: min(len(providers), _gpt55_provider_limit())]


def _gpt55_call_timeout_seconds(purpose: str) -> float:
    raw_timeout = os.environ.get(_GPT55_CALL_TIMEOUT_ENV)
    if raw_timeout:
        try:
            return max(1.0, float(raw_timeout))
        except (TypeError, ValueError):
            logger.warning(
                "invalid %s=%r; using purpose default",
                _GPT55_CALL_TIMEOUT_ENV,
                raw_timeout,
            )
    if purpose == "apparel_scene_director":
        return _GPT55_DIRECTOR_TIMEOUT_SEC
    if purpose == "apparel_prompt_composer":
        return _GPT55_COMPOSER_TIMEOUT_SEC
    if purpose == "apparel_prompt_risk_review":
        return _GPT55_REVIEW_TIMEOUT_SEC
    logger.warning(
        "unknown GPT-5.5 call purpose=%r; using default timeout %gs",
        purpose,
        _GPT55_DEFAULT_TIMEOUT_SEC,
    )
    return _GPT55_DEFAULT_TIMEOUT_SEC


async def _call_responses_text_with_timeout(
    *,
    provider: ProviderDefinition,
    attempt: dict[str, Any],
    purpose: str,
    instructions: str,
    payload: dict[str, Any],
    max_output_tokens: int,
    reference_images: list[dict[str, str]] | None = None,
    timeout_seconds: float,
) -> str:
    try:
        return await asyncio.wait_for(
            _call_responses_text(
                provider=provider,
                attempt=attempt,
                purpose=purpose,
                instructions=instructions,
                payload=payload,
                max_output_tokens=max_output_tokens,
                reference_images=reference_images,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise _Gpt55CallTimeout(f"timed out after {timeout_seconds:g}s") from exc


async def _call_responses_text(
    *,
    provider: ProviderDefinition,
    attempt: dict[str, Any],
    purpose: str,
    instructions: str,
    payload: dict[str, Any],
    max_output_tokens: int,
    reference_images: list[dict[str, str]] | None = None,
) -> str:
    content: list[dict[str, Any]] = []
    for index, ref in enumerate(reference_images or [], start=1):
        image_url = str(ref.get("image_url") or ref.get("url") or "").strip()
        if not image_url:
            continue
        label = clean_text(ref.get("label"), max_len=80) or f"参考图 {index}"
        content.extend(
            [
                {
                    "type": "input_text",
                    "text": (
                        f"参考图 {index}：{label}。请只用于观察搭配、模特气质、"
                        "比例、姿态和摄影适配，不要在输出中复述图片细节。"
                    ),
                },
                {"type": "input_image", "image_url": image_url},
            ]
        )
    content.append(
        {
            "type": "input_text",
            "text": json.dumps(payload, ensure_ascii=False),
        }
    )
    body: dict[str, Any] = {
        "model": attempt["model"],
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "stream": False,
        "store": False,
        "max_output_tokens": max_output_tokens,
        "metadata": {"purpose": purpose},
    }
    if attempt.get("reasoning"):
        body["reasoning"] = attempt["reasoning"]
    if attempt.get("service_tier"):
        body["service_tier"] = attempt["service_tier"]

    proxy_url = await resolve_provider_proxy_url(provider.proxy)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=8.0, read=70.0, write=30.0, pool=8.0),
        proxy=proxy_url,
    ) as client:
        resp = await client.post(
            responses_url(provider.base_url),
            json=body,
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code >= 400:
        detail = resp.text[:500]
        raise _UpstreamHTTPError(resp.status_code, detail)
    try:
        response_payload = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("upstream returned invalid JSON") from exc
    text = extract_response_text(response_payload)
    if not text:
        raise ValueError("upstream returned empty text")
    return text


class _UpstreamHTTPError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"http {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def _should_retry_without_reference_images(exc: Exception) -> bool:
    if not isinstance(exc, _UpstreamHTTPError):
        return False
    if exc.status_code not in _REFERENCE_IMAGE_RETRY_STATUS:
        return False
    detail = str(getattr(exc, "detail", "") or exc).lower()
    return any(token in detail for token in _REFERENCE_IMAGE_RETRY_TOKENS)


def _should_try_next_attempt(exc: Exception) -> bool:
    if isinstance(exc, _UpstreamHTTPError):
        if exc.status_code in _RETRYABLE_STATUS:
            return True
        if 400 <= exc.status_code < 500:
            return False
        return True
    return True


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("JSON root must be object")
    return data


__all__ = [
    "ContinuityAnchor",
    "ScenePlannerMode",
    "SceneStrategy",
    "SceneVariety",
    "build_garment_lock",
    "coerce_bool",
    "clean_text",
    "compose_image_prompt_with_gpt55",
    "fallback_prompt_composition",
    "fallback_risk_review",
    "fallback_scene_cards_from_pool",
    "plan_scene_cards_with_gpt55",
    "review_prompt_risk_with_gpt55",
    "resolve_scene_provider_order",
    "rules_fallback_planning",
    "scene_fingerprint",
]
