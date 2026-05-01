"""GenerationErrorCode enum 契约测试。

字符串值是上游 / DB / 前端的稳定接口；enum 改名时 .value 必须保持，
否则 retry.py 的 _TERMINAL_ERROR_CODES / _RETRIABLE_ERROR_CODES 会出现"代码改了
但行为没改"的错位。
"""

from __future__ import annotations

from lumen_core.constants import (
    GENERATION_STAGE_FALLBACK,
    GenerationErrorCode,
    GenerationStage,
)


def test_enum_values_are_lowercase_snake() -> None:
    for code in GenerationErrorCode:
        assert code.value == code.value.lower()
        assert " " not in code.value
        assert code.value.replace("_", "").isalnum()


def test_enum_values_are_unique() -> None:
    values = [c.value for c in GenerationErrorCode]
    assert len(values) == len(set(values))


def test_enum_is_str_compatible() -> None:
    """StrEnum 必须能直接和 str 比较，否则落库的 .value 会跟历史数据脱节。"""
    assert GenerationErrorCode.RATE_LIMIT_ERROR == "rate_limit_error"
    assert str(GenerationErrorCode.INVALID_VALUE) == "invalid_value"


def test_critical_codes_present() -> None:
    """生图 / completion / fallback 三条主链路依赖的核心错误码必须存在。
    新增字段时不要动这些 enum 名 —— retry.py 的判定基于这些。"""
    must_have = {
        "rate_limit_error",
        "invalid_value",
        "no_image_returned",
        "stream_interrupted",
        "sse_curl_failed",
        "bad_response",
        "all_providers_failed",
        "responses_fallback_failed",
        "fallback_lanes_failed",
        "all_accounts_failed",
        "provider_exhausted",
        "moderation_blocked",
        "bad_reference_image",
        "disk_full",
        "local_queue_full",
    }
    actual = {c.value for c in GenerationErrorCode}
    missing = must_have - actual
    assert not missing, f"missing critical error codes: {missing}"


def test_enum_fits_db_column() -> None:
    """Generation.error_code 是 String(64)；enum value 不能超过这个长度。"""
    for code in GenerationErrorCode:
        assert len(code.value) <= 64, f"{code.name} value too long: {code.value}"


# --- GenerationStage 粗/细 双层 ---

def test_generation_stage_includes_coarse_levels() -> None:
    """粗阶段是 Generation.progress_stage 持久化写入的值，必须保留向后兼容。"""
    must_have = {"queued", "understanding", "rendering", "finalizing"}
    actual = {s.value for s in GenerationStage}
    assert must_have <= actual


def test_generation_stage_includes_substages() -> None:
    """P1 细颗粒度：DevelopingCard 显影动画依赖这些子阶段。"""
    must_have = {
        "provider_selected",
        "stream_started",
        "partial_received",
        "final_received",
        "processing",
        "storing",
    }
    actual = {s.value for s in GenerationStage}
    assert must_have <= actual


def test_substage_fallback_map_complete() -> None:
    """每个细子阶段都必须有粗阶段降级；前端不识别 substage 时按 stage 走。"""
    substage_values = {
        "provider_selected",
        "stream_started",
        "partial_received",
        "final_received",
        "processing",
        "storing",
    }
    assert set(GENERATION_STAGE_FALLBACK.keys()) == substage_values
    coarse_values = {"queued", "understanding", "rendering", "finalizing"}
    for sub, coarse in GENERATION_STAGE_FALLBACK.items():
        assert coarse in coarse_values, f"{sub} → {coarse} not a coarse stage"


def test_generation_stage_values_fit_db_column() -> None:
    """Generation.progress_stage 是 String(32)。"""
    for stage in GenerationStage:
        assert len(stage.value) <= 32, f"{stage.name} value too long"
