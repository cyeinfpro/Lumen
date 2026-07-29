"""GPT scene planning orchestration and director prompt construction."""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.apparel_scene_fallbacks import (
    clean_text,
    coerce_string_list,
    compact_product_context_for_gpt55,
)
from .contracts import SceneProviderSelection

GPTJsonCall = Callable[..., Awaitable[dict[str, Any]]]
NormalizeSceneCards = Callable[
    [Any, list[tuple[str, dict[str, Any]]]],
    list[dict[str, Any]],
]
FallbackSceneCards = Callable[..., list[dict[str, Any]]]
FallbackPlanningResult = Callable[..., dict[str, Any]]
UniqueFingerprints = Callable[[list[dict[str, Any]]], list[str]]

DIRECTOR_RETRY_ENV = "LUMEN_SHOWCASE_GPT_DIRECTOR_RETRIES"
DIRECTOR_DEFAULT_RETRIES = 1


@dataclass(frozen=True, slots=True)
class PlanningDependencies:
    call_json: GPTJsonCall
    normalize_scene_cards: NormalizeSceneCards
    fallback_scene_cards: FallbackSceneCards
    fallback_result: FallbackPlanningResult
    unique_fingerprints: UniqueFingerprints
    director_retry_count: Callable[[], int]
    logger: logging.Logger


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
    provider_selection: SceneProviderSelection | None,
    reference_images: list[dict[str, str]] | None,
    dependencies: PlanningDependencies,
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
    instructions = director_instructions(output_count)
    retry_errors: list[str] = []
    retry_rounds = 1 + dependencies.director_retry_count()
    last_error = ""
    for round_index in range(retry_rounds):
        try:
            raw = await dependencies.call_json(
                db,
                purpose="apparel_scene_director",
                instructions=director_retry_instructions(
                    instructions,
                    round_index=round_index,
                    last_error=last_error,
                ),
                payload=director_retry_payload(
                    payload,
                    round_index=round_index,
                    last_error=last_error,
                ),
                max_output_tokens=5200 if output_count <= 8 else 9000,
                provider_selection=provider_selection,
                reference_images=reference_images,
            )
            cards = dependencies.normalize_scene_cards(
                raw.get("scene_cards"),
                shot_picks,
            )
            if len(cards) != output_count:
                raise ValueError("scene card count mismatch")
            fingerprints = dependencies.unique_fingerprints(cards)
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
                dependencies.logger.warning(
                    "apparel scene director retry %s/%s after failure: %s",
                    round_index + 2,
                    retry_rounds,
                    last_error,
                )
                continue
            dependencies.logger.warning(
                "apparel scene director fallback after %s rounds: %s",
                retry_rounds,
                last_error,
            )
    fallback_cards = dependencies.fallback_scene_cards(
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
    fallback = dependencies.fallback_result(
        fallback_cards,
        reason=f"gpt55_director_retry_exhausted: {last_error}",
    )
    fallback["director_attempts_made"] = len(retry_errors)
    fallback["director_retry_count"] = len(retry_errors)
    fallback["director_retry_errors"] = retry_errors
    return fallback


def gpt55_director_retry_count(logger: logging.Logger) -> int:
    raw_retries = os.environ.get(DIRECTOR_RETRY_ENV)
    if raw_retries:
        try:
            return max(0, min(5, int(raw_retries)))
        except (TypeError, ValueError):
            logger.warning(
                "invalid %s=%r; using default",
                DIRECTOR_RETRY_ENV,
                raw_retries,
            )
    return DIRECTOR_DEFAULT_RETRIES


def director_retry_payload(
    payload: dict[str, Any],
    *,
    round_index: int,
    last_error: str,
) -> dict[str, Any]:
    if round_index <= 0:
        return payload
    failure_summary = director_retry_failure_summary(last_error)
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


def director_retry_instructions(
    instructions: str,
    *,
    round_index: int,
    last_error: str,
) -> str:
    if round_index <= 0:
        return instructions
    error = director_retry_failure_summary(last_error)
    return (
        f"{instructions}\n\n"
        f"【重试修正】这是第 {round_index + 1} 轮导演请求。上一轮失败原因：{error}。"
        "这一次必须针对失败原因完整修正并重新输出整批 JSON，不要只输出补丁。"
        "所有 required fields 都要具体可拍摄；micro_event、pose、motion 不能写成"
        "自然站姿、正面全身、商品展示这类泛化词；不得重复地点、动作或构图；"
        "除 shot_class=side_or_back 外，不得把主视角写成背影、背向、后背或纯侧面。"
    )


def director_retry_failure_summary(error: str) -> str:
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


def director_instructions(output_count: int) -> str:
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
