"""Pure generation helpers for the model-library workflow routes."""

from __future__ import annotations

from typing import Any, Callable


MODEL_LIBRARY_TITLE_AGE_LABELS: dict[str, str] = {
    "user_favorites": "收藏",
    "toddler": "幼儿",
    "child": "儿童",
    "teen": "青少年",
    "young_adult": "青年",
    "adult": "熟龄",
    "middle_aged": "中年",
    "senior": "老年",
}


def model_library_gender_label(genders: list[str]) -> str:
    if set(genders) == {"female", "male"}:
        return "男女"
    if not genders:
        return "女性"
    return "女性" if genders[0] == "female" else "男性"


def model_library_run_title(
    *,
    age_segment: str | None,
    gender: str | None = None,
    genders: list[str] | None = None,
    appearance_direction: str | None,
    mode: str = "text",
    gender_label: Callable[[list[str]], str],
) -> str:
    age_key = age_segment or "young_adult"
    age_label = MODEL_LIBRARY_TITLE_AGE_LABELS.get(age_key, age_key)
    resolved_gender_label = gender_label(genders or ([gender] if gender else []))
    appearance = (appearance_direction or "").strip()
    prefix = "参考图生成" if mode == "reference_image" else "模特库生成"
    parts = [f"{age_label}{resolved_gender_label}"]
    if appearance:
        parts.append(appearance[:24])
    title = " · ".join([prefix, *parts])
    return title[:120]


def model_library_generate_prompt(
    *,
    age_segment: str,
    gender: str,
    appearance_direction: str | None,
    extra_requirements: str | None,
    style_tags: list[str],
    candidate_index: int,
    reference_mode: bool,
    clean_style_tags: Callable[[Any], list[str]],
    model_diversity_anchor: Callable[..., str],
) -> str:
    """Build one 2x2 contact-sheet prompt for a model-library candidate."""
    gender_label = "female" if gender == "female" else "male"
    appearance = (appearance_direction or "").strip()
    extras = (extra_requirements or "").strip()
    tag_text = ", ".join(clean_style_tags(style_tags)) if style_tags else ""
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
    appearance_directive = f"Appearance direction: {appearance}." if appearance else ""
    style_directive = f"Style references: {tag_text}." if tag_text else ""
    extras_directive = f"User notes: {extras}." if extras else ""
    diversity = (
        ""
        if reference_mode
        else model_diversity_anchor(
            candidate_index=candidate_index,
            gender=gender,
            age_segment=age_segment,
        )
    )
    reference_directive = (
        "Use the attached reference image ONLY as the identity lock for the SAME "
        "PERSON that must appear in all four panels. Preserve the reference "
        "person's facial structure, eye shape, nose, mouth, eyebrows, skin tone, "
        "hair color, hair length and hair style as faithfully as possible. The "
        "reference image may be from any angle, expression, crop, or composition "
        "(front, three-quarter, profile, back, close-up, candid). Do not copy the "
        "reference pose, framing, background, clothing, or expression. Infer unseen "
        "sides of the head and body from the reference, and re-render the same "
        "person in the required front, true left profile, back, and frontal "
        "headshot views with a neutral relaxed expression and closed mouth. If the "
        "reference only shows the face or upper body, infer plausible full-body "
        "proportions consistent with the inferred age and gender."
        if reference_mode
        else ""
    )
    return " ".join(
        part
        for part in [
            "Create one clean 2x2 ecommerce model reference contact sheet, exactly four panels: "
            "top-left front full body, top-right left 90-degree profile full body, "
            "bottom-left straight back full body, bottom-right close-up headshot.",
            reference_directive,
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


def model_library_job_status(
    *,
    step_status: str,
    requested_count: int,
    finished_count: int,
) -> str:
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
