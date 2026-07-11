"""Pure workflow-output parsing and aggregation helpers."""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from lumen_core.models import Generation, ModelCandidate, QualityReport

from .serialization import _dedupe_nonempty, _dict_or_empty


MODEL_CANDIDATE_COUNT = 3

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


def _generation_batch_outcome(
    *,
    ready_count: int,
    active_count: int,
    expected_count: int,
) -> str:
    if ready_count >= max(1, expected_count):
        return "complete"
    if active_count > 0:
        return "running"
    if ready_count > 0:
        return "partial"
    return "failed"


def _failed_generation_output(
    existing: dict[str, Any] | None,
    failed_generations: Iterable[Generation],
    *,
    fallback: str,
    partial: bool,
) -> dict[str, Any]:
    failed = list(failed_generations)
    output = dict(existing or {})
    output["failed_generation_ids"] = [generation.id for generation in failed]
    output["error_message"] = _task_error_summary(failed, fallback)
    if partial:
        output["partial"] = True
    else:
        output.pop("partial", None)
    return output


def _candidate_generated_image_ids(candidate: ModelCandidate) -> list[str]:
    brief = _dict_or_empty(candidate.model_brief_json)
    raw_ids = brief.get("candidate_image_ids")
    if isinstance(raw_ids, list):
        return [image_id for image_id in raw_ids if isinstance(image_id, str)]
    return (
        [candidate.contact_sheet_image_id]
        if isinstance(candidate.contact_sheet_image_id, str)
        else []
    )


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
            any(key in item for key in ("type", "text", "content"))
            for item in dict_items
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
            str(item) for item in visible_bits if item not in (None, "", "unknown")
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
                "message": str(
                    parsed.get("summary_text")
                    or text
                    or "QC review did not return issue details."
                ),
            }
        ]
    recommendation = str(parsed.get("recommendation") or "review").strip().lower()
    if recommendation not in {"approve", "revise"}:
        recommendation = "revise"
    return {
        "overall_score": _clamp_score(parsed.get("overall_score"), 70),
        "product_fidelity_score": _clamp_score(
            parsed.get("product_fidelity_score"),
            70,
        ),
        "model_consistency_score": _clamp_score(
            parsed.get("model_consistency_score"),
            70,
        ),
        "aesthetic_score": _clamp_score(parsed.get("aesthetic_score"), 70),
        "artifact_score": _clamp_score(parsed.get("artifact_score"), 70),
        "issues_json": [item for item in issues if isinstance(item, dict)]
        or [
            {
                "severity": "medium",
                "type": "quality_review",
                "message": "QC review returned no structured issues.",
            }
        ],
        "recommendation": recommendation,
    }


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
