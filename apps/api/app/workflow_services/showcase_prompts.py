"""Showcase prompt composition, garment locking, and safety rewrites."""

from __future__ import annotations

from typing import Any

from lumen_core.models import ModelCandidate

from ..routes._showcase_shot_pool import ShotVariant
from .showcase_runtime import runtime as _runtime


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
    scene_card_mode: bool = False,
    allow_pet: bool = True,
    allow_background_people: bool = True,
    include_product_lock: bool = True,
) -> str:
    runtime = _runtime()
    direction = template_direction.strip() or "背景与衣服图片搭配"
    extra_direction = runtime._compact_showcase_user_direction(
        user_direction,
        style_region,
    )
    if extra_direction:
        direction = f"{direction}；{extra_direction}"
    photography_direction = (
        "1. 摄影执行：严格按本张拍摄方案的地点、生活事件、机位、距离、镜头感、"
        "动作和动态执行，允许真实低角度、俯拍、近距离手机抓拍或环境远景；"
        "必须保持头身比例自然、透视可信，动作像真实抓拍；不得退回普通棚拍站姿，"
        "不得和其它图片重复同一地点、同一站姿、同一手部动作；"
        "避免跳跃、转圈、跪趴、后仰、大幅甩头。"
        if scene_card_mode
        else "1. 摄影执行：真实模特目录摄影，约 50mm 标准焦段（不要广角拉头身比、不要长焦压扁），平视或胸口高度机位；身体重心可信，动作幅度小，避免跳跃、转圈、跪趴、后仰、大幅甩头。"
    )
    if scene_card_mode:
        extras: list[str] = []
        scene_text = template_direction
        if allow_pet and any(
            token in scene_text
            for token in ("宠物", "狗", "猫", "牵引绳", "小狗", "小猫")
        ):
            extras.append("低存在感宠物")
        if allow_background_people and any(
            token in scene_text for token in ("路人", "人群", "行人")
        ):
            extras.append("远处路人")
        extras.append("生活道具作为环境辅助")
        subject_rule = (
            f"9. 主角只有一位已确认模特；可有{'、'.join(extras)}，"
            "但不得抢主体或遮挡商品。"
        )
    else:
        subject_rule = "9. 单人照。"
    lines = [
        "请根据这张白底产品图和模特图，生成真实自然的真人模特穿搭图。",
        "",
    ]
    if include_product_lock:
        compact_product_preserve = runtime._compact_lock_text(product_preserve)
        lines.extend(
            [
                "【商品 1:1 锁定】以商品图为准，保持同款同色同廓形；"
                f"本张重点清楚：{compact_product_preserve or '商品主体'}。"
                "不得改款改色，不增删图案/logo、口袋、扣件或缝线。",
                "",
            ]
        )
    lines.extend(
        [
            "要求：",
            photography_direction,
            "2. 模特按产品图自然穿着这件衣服，版型贴合，褶皱合理；不要人偶感、不要时装秀台步。",
            f"3. 模特参考模特图，{model_consistency}身材和表情自然。",
            f"4. 配饰：{accessory_direction}（不得遮挡商品）。",
            f"5. 场景：背景与衣服风格搭配，{direction}；画面里不要出现镜子或镜面反射。",
            f"6. 画质：{quality_direction}，{render_direction}。",
            f"7. 构图：{style_region}风格，{pose_direction}，{shot_direction}。",
            f"8. 画面：{framing_direction}；服装主体清晰可见。",
            subject_rule,
        ]
    )
    return "\n".join(lines)


def _showcase_garment_lock_prefix(
    *,
    garment_lock: dict[str, Any] | None,
    product_preserve: str,
    model_consistency: str,
    visible_preserve: str | None = None,
    deferred_preserve: str | None = None,
) -> str:
    runtime = _runtime()
    visible_line = (visible_preserve or "").strip()
    deferred_line = (deferred_preserve or "").strip()
    compact_visible = runtime._compact_lock_text(
        visible_line or product_preserve,
        max_items=5,
    )
    compact_deferred_note = (
        "\n【其它细节】其它角度细节交给其它图，本张不为它们牺牲自然动作。"
        if deferred_line
        else ""
    )
    if not isinstance(garment_lock, dict):
        text = (
            "【商品 1:1 锁定】以商品图为准，保持同款同色同廓形；"
            f"本张重点清楚：{compact_visible or '商品主体'}。"
            "不得改款改色，不增删图案/logo、口袋、扣件或缝线；手、头发和道具不遮挡主体。\n"
            f"【模特一致】{model_consistency}"
        )
        return f"{text}{compact_deferred_note}"
    identity = runtime._compact_product_identity(
        garment_lock,
        product_preserve,
    )
    visibility = runtime._join_lock_items(
        garment_lock.get("visibility_priority"),
        max_items=3,
    )
    visible_text = compact_visible or visibility or "商品主体"
    text = (
        f"【商品 1:1 锁定】以商品图为准还原{identity}，保持同款同色同廓形；"
        f"本张重点清楚：{visible_text}。"
        "不得改款改色，不增删图案/logo、口袋、扣件或缝线；手、头发和道具不遮挡主体。\n"
        f"【模特一致】{model_consistency}"
    )
    return f"{text}{compact_deferred_note}"


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
    scene_card: dict[str, Any] | None = None,
    garment_lock: dict[str, Any] | None = None,
    composed_prompt: str | None = None,
    allow_pet: bool = True,
    allow_background_people: bool = True,
) -> str:
    runtime = _runtime()
    brief = selected_candidate.model_brief_json or {}
    summary = str(brief.get("summary") or user_prompt or "自然电商模特")
    must_preserve = product_analysis.get("must_preserve")
    fallback_preserve = (
        "颜色、版型、款式、领口、袖型、衣长、图案/logo、纽扣/拉链/口袋/缝线"
    )
    preserve_items = (
        [str(item).strip() for item in must_preserve if str(item).strip()]
        if isinstance(must_preserve, list)
        else []
    )
    product_preserve = "、".join(preserve_items[:8]) or fallback_preserve
    visible_preserve, deferred_preserve = runtime._showcase_visibility_policy(
        garment_lock=garment_lock,
        product_preserve=product_preserve,
        scene_card=scene_card,
        shot_type=shot_type,
    )
    height_cm_raw = brief.get("height_cm")
    try:
        height_cm = (
            int(height_cm_raw)
            if height_cm_raw is not None
            else runtime._infer_model_height_cm(
                " ".join(part for part in (summary, user_prompt) if part)
            )
        )
    except (TypeError, ValueError):
        height_cm = runtime._infer_model_height_cm(
            " ".join(part for part in (summary, user_prompt) if part)
        )
    model_consistency = (
        "保持同一张脸、发型、肤色、年龄感和身材比例一致，不要换人。"
        f"身高 {height_cm}cm，头身比和肢体长度沿用参考模特。"
        f"模特方向：{summary}。"
    )
    accessory_direction = (
        "少量自然搭配，不要抢衣服主体；如果附件中包含已选配饰四宫格，优先参考它。"
    )
    if shot_variant is None:
        shot_variant = runtime._showcase_default_variant(
            template,
            shot_type,
            age_segment,
        )
    shot_direction = shot_variant["label"] if shot_variant else shot_type
    framing = shot_variant["framing"] if shot_variant else "product_first"
    framing_direction = runtime._showcase_framing_direction(
        shot_type,
        framing,
        aspect_ratio,
    )
    composition_extra = runtime._showcase_composition_direction(template)
    if composition_extra:
        framing_direction = f"{framing_direction}；{composition_extra}"
    pose_direction = runtime._showcase_pose_direction(template)
    soft = runtime._age_soft_constraint(age_segment)
    if soft:
        pose_direction = f"{pose_direction}，{soft}"
    quality_direction = "4K 终稿" if final_quality == "4k" else "高质量"
    style_region = runtime._style_region_from_text(summary)
    lock_prefix = (
        runtime._showcase_garment_lock_prefix(
            garment_lock=garment_lock,
            product_preserve=product_preserve,
            model_consistency=model_consistency,
            visible_preserve=visible_preserve,
            deferred_preserve=deferred_preserve,
        )
        if garment_lock is not None
        else ""
    )
    scene_direction = runtime._showcase_scene_card_direction(scene_card)
    if composed_prompt and composed_prompt.strip():
        prefix = (
            f"{lock_prefix}\n\n【本张拍摄方案】\n"
            if lock_prefix
            else "【本张拍摄方案】\n"
        )
        if len(prefix) > runtime.MAX_PROMPT_CHARS - 600:
            prefix = runtime._truncate_prompt_text(
                prefix,
                runtime.MAX_PROMPT_CHARS - 600,
            )
        body = composed_prompt.strip()
        if scene_direction:
            seed_parts: list[str] = []
            if isinstance(scene_card, dict):
                camera = runtime._dict_or_empty(scene_card.get("camera"))
                camera_seed = "，".join(
                    runtime._showcase_scene_label(item)
                    for item in (
                        camera.get("distance"),
                        camera.get("angle"),
                        camera.get("lens_feel"),
                    )
                    if str(item or "").strip()
                )
                seed_parts = [
                    str(scene_card.get("location") or "").strip(),
                    str(scene_card.get("micro_event") or "").strip(),
                    camera_seed,
                ]
            seed_line = "；".join(part for part in seed_parts if part)
            scene_rules = [
                "【本张拍摄方案必须执行】",
                "最终画面只采用上方短摄影方案的场景、动作、神态、构图、光线和镜头；"
                "不得混入其它地点、动作、旧模板文案或普通棚拍站姿。",
                (
                    "商品主体清楚，不遮挡。"
                    if lock_prefix
                    else "本张商品重点："
                    f"{runtime._compact_lock_text(visible_preserve) or visible_preserve}。"
                ),
            ]
            if shot_type != "side_or_back":
                scene_rules.append(
                    "本张视角：正面或三分之二正面优先，脸和商品主体清楚；"
                    "不要背影、背向或以后背作为主视角。"
                )
            if seed_line:
                scene_rules.append(f"本张场景种子：{seed_line}。")
            if deferred_preserve:
                scene_rules.append("其它角度细节交给其它图，不要为它们破坏当前镜头。")
            body = f"{body}\n\n" + "".join(scene_rules)
        return prefix + body[: max(0, runtime.MAX_PROMPT_CHARS - len(prefix))]
    template_direction = runtime._template_requirement(
        template, product_analysis, scene_environment
    )
    render_direction = runtime._showcase_render_direction(
        template,
        scene_environment,
    )
    if scene_direction:
        scene_only_direction = runtime._showcase_scene_card_scene_direction(scene_card)
        template_direction = scene_only_direction or scene_direction
        render_direction = runtime._showcase_scene_render_direction(
            scene_card,
            age_segment=age_segment,
            model_summary=summary,
        )
        framing_direction = runtime._showcase_scene_framing_direction(
            scene_card, framing_direction
        )
        shot_direction = f"本张可见性目标：{visible_preserve}"
        if shot_type != "side_or_back":
            shot_direction = (
                "正面或三分之二正面，脸和商品主体清楚；不要背影或后背主视角；"
                f"{shot_direction}"
            )
        if deferred_preserve:
            shot_direction = f"{shot_direction}；其它角度细节不强求"
        camera_direction = runtime._showcase_scene_card_camera_direction(scene_card)
        if camera_direction:
            shot_direction = f"{camera_direction}；{shot_direction}"
        pose_direction = (
            runtime._showcase_scene_card_action_direction(scene_card)
            or "只执行本张拍摄方案的动作和动态，不混入其它模板动作或旧 shot 文案"
        )
    body = runtime._showcase_prompt_brief(
        user_direction=user_prompt,
        template_direction=template_direction,
        product_preserve=visible_preserve,
        accessory_direction=accessory_direction,
        model_consistency=model_consistency,
        shot_direction=shot_direction,
        pose_direction=pose_direction,
        framing_direction=framing_direction,
        quality_direction=quality_direction,
        render_direction=render_direction,
        style_region=style_region,
        scene_card_mode=bool(scene_direction),
        allow_pet=allow_pet,
        allow_background_people=allow_background_people,
        include_product_lock=not bool(lock_prefix),
    )
    if not lock_prefix:
        return body
    prefix = f"{lock_prefix}\n\n"
    if len(prefix) > runtime.MAX_PROMPT_CHARS - 600:
        prefix = runtime._truncate_prompt_text(
            prefix,
            runtime.MAX_PROMPT_CHARS - 600,
        )
    return prefix + body[: max(0, runtime.MAX_PROMPT_CHARS - len(prefix))]


def _composition_shooting_brief(composition: dict[str, Any]) -> str:
    if not isinstance(composition, dict) or composition.get("status") == "fallback":
        return ""
    brief = str(
        composition.get("shooting_brief") or composition.get("final_prompt") or ""
    ).strip()
    if not brief:
        return ""
    full_prompt_markers = (
        "【最高优先级：商品",
        "【商品 1:1",
        "请根据这张白底产品图",
    )
    if any(marker in brief for marker in full_prompt_markers):
        return ""
    return brief


def _guarded_shooting_brief(
    shooting_brief: str,
    *,
    rewrite_instruction: str,
) -> str:
    runtime = _runtime()
    brief = shooting_brief.strip()
    instruction = runtime._preserve_safe_motion_rewrite_instruction(
        rewrite_instruction.strip()
    ) or (
        "简化动作和道具关系，避免任何手、头发、宠物、饮料杯、手机、包带或前景物遮挡商品主体。"
    )
    replaces_scene = runtime._rewrite_instruction_replaces_scene_or_composition(
        instruction
    )
    lead = (
        "安全覆盖：以本安全覆盖重写当前拍摄方案；允许更换场景、构图、光线、镜头、"
        "空间层次、自然情绪、动作和道具；"
        if replaces_scene
        else "安全覆盖：上方摄影方案只保留与本安全覆盖不冲突的安全元素；"
    )
    guard = lead + (
        f"只把可能造成遮挡、改款或动作过复杂的部分按此改写：{instruction}。"
        "保留安全动态能量，优先使用走近、落步、半转回头、双手低位摆动、"
        "衣摆或发丝轻动等不会遮挡商品主体的抓拍瞬间；不要退回僵硬静态站姿。"
        "如上方摄影方案与本安全覆盖冲突，以本安全覆盖为准；"
        "手、头发、道具和前景不得遮挡商品主体，当前角度可见的服装结构、领口、袖口、"
        "口袋、图案/扣件和布料纹理必须清楚。"
    )
    if replaces_scene:
        return guard
    return f"{brief}\n\n{guard}" if brief else guard


def _preserve_safe_motion_rewrite_instruction(instruction: str) -> str:
    runtime = _runtime()
    text = instruction.strip()
    if not text:
        return ""
    for static_text, dynamic_text in runtime._STATIC_REWRITE_REPLACEMENTS:
        text = text.replace(static_text, dynamic_text)
    static_tokens = (
        "稳定站定",
        "站定展示",
        "静态站姿",
        "普通站姿",
        "僵硬静态",
    )
    if any(token in text for token in static_tokens):
        text = (
            f"{text}；同时必须保留安全动态抓拍感：脚步刚落地、半转回头或向镜头走近，"
            "手、头发和道具避开商品主体。"
        )
    return text


def _rewrite_instruction_replaces_scene_or_composition(instruction: str) -> bool:
    text = instruction.strip().lower()
    if not text:
        return False
    preserve_phrases = (
        "不要改变场景",
        "不用改变场景",
        "无需改变场景",
        "不改变场景",
        "保留场景",
        "保留地点",
        "keep the scene",
        "do not change the scene",
        "don't change the scene",
    )
    if any(phrase in text for phrase in preserve_phrases):
        return False
    scene_terms = (
        "场景",
        "地点",
        "环境",
        "构图",
        "机位",
        "镜头",
        "光线",
        "scene",
        "location",
        "setting",
        "composition",
        "framing",
        "camera",
        "lighting",
    )
    change_terms = (
        "改",
        "换",
        "替换",
        "更换",
        "重新",
        "另一",
        "另一个",
        "新的",
        "不同",
        "避免重复",
        "重复",
        "change",
        "replace",
        "switch",
        "rewrite",
        "new",
        "different",
        "avoid repetition",
        "repetition",
    )
    return any(term in text for term in scene_terms) and any(
        term in text for term in change_terms
    )


_STATIC_REWRITE_REPLACEMENTS = (
    (
        "改为稳定的正面或三分之二正面站定展示",
        "改为正面或三分之二正面的安全动态抓拍，脚步刚落地，双手低位避开商品主体",
    ),
    (
        "稳定的正面或三分之二正面站定展示",
        "正面或三分之二正面的安全动态抓拍，脚步刚落地，双手低位避开商品主体",
    ),
    (
        "稳定站定展示",
        "安全动态抓拍，脚步刚落地，身体重心有真实变化",
    ),
    (
        "站定展示",
        "安全动态展示，脚步刚落地，身体重心有真实变化",
    ),
    (
        "静态展示",
        "安全动态抓拍",
    ),
    (
        "只保留轻微落步感",
        "保留清楚但安全的落步动态，脚步、衣摆和发丝都有自然方向感",
    ),
    (
        "双手远离胸口和裙身主体",
        "双手保持低位或打开在身体两侧，避开胸口、图案、口袋和裙身主体",
    ),
    (
        "双手远离胸口和衣身主体",
        "双手保持低位或打开在身体两侧，避开胸口、图案、口袋和衣身主体",
    ),
)
