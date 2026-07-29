"""Prompt composition and risk-review implementations."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.constants import MAX_PROMPT_CHARS

from ...domain.apparel_scene_fallbacks import (
    clean_text,
    coerce_bool,
    coerce_string_list,
    compact_product_context_for_gpt55,
    dict_or_empty as _dict_or_empty,
)
from .contracts import SceneProviderSelection
from .parsing_validation import (
    coerce_candidate_briefs,
    coerce_selection_scores,
    sanitize_shooting_brief,
)

GPTJsonCall = Callable[..., Awaitable[dict[str, Any]]]
FallbackBuilder = Callable[..., dict[str, Any]]


@dataclass(frozen=True, slots=True)
class PromptCompositionDependencies:
    call_json: GPTJsonCall
    fallback_prompt: FallbackBuilder
    fallback_risk_review: FallbackBuilder
    logger: logging.Logger


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
    rewrite_instruction: str | None,
    provider_selection: SceneProviderSelection | None,
    reference_images: list[dict[str, str]] | None,
    dependencies: PromptCompositionDependencies,
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
        raw = await dependencies.call_json(
            db,
            purpose="apparel_prompt_composer",
            instructions=instructions,
            payload=payload,
            max_output_tokens=1400,
            provider_selection=provider_selection,
            reference_images=reference_images,
        )
        shooting_brief = sanitize_shooting_brief(
            raw.get("shooting_brief") or raw.get("final_prompt"),
            max_len=min(1800, MAX_PROMPT_CHARS),
        )
        candidate_briefs = coerce_candidate_briefs(raw.get("candidate_briefs"))
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
            "selection_scores": coerce_selection_scores(raw.get("selection_scores")),
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
        dependencies.logger.warning("apparel prompt composer fallback: %s", exc)
        return dependencies.fallback_prompt(
            base_prompt=base_prompt,
            scene_card=scene_card,
            reason=str(exc),
        )


async def review_prompt_risk_with_gpt55(
    db: AsyncSession,
    *,
    final_prompt: str,
    garment_lock: dict[str, Any],
    scene_card: dict[str, Any],
    batch_context: dict[str, Any],
    provider_selection: SceneProviderSelection | None,
    dependencies: PromptCompositionDependencies,
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
        raw = await dependencies.call_json(
            db,
            purpose="apparel_prompt_risk_review",
            instructions=instructions,
            payload=payload,
            max_output_tokens=900,
            provider_selection=provider_selection,
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
        dependencies.logger.warning("apparel risk review fallback: %s", exc)
        return dependencies.fallback_risk_review(
            scene_card=scene_card,
            reason=str(exc),
        )
