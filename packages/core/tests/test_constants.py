"""upstream error 分类层（V1.0 新增）的契约测试。

覆盖：
- 新增 UPSTREAM_* enum 必须存在且字符串值唯一
- UPSTREAM_TYPE_TO_CODE 对官方 4 类 type 都有覆盖
- classify_upstream_error 的优先级：type > code > status > UNKNOWN
- 边界：大小写、空字符串、未知 type / status

GenerationErrorCode 整体字段契约见 test_generation_error_code.py，这里只关心
classify_upstream_error 与新增 UPSTREAM_* 码。
"""

from __future__ import annotations

import pytest

from lumen_core.constants import (
    UPSTREAM_CODE_TO_CODE,
    UPSTREAM_TYPE_TO_CODE,
    GenerationErrorCode,
    classify_upstream_error,
)


# --- 新增 UPSTREAM_* 枚举值存在性 ---


def test_upstream_codes_present() -> None:
    """新增的上游分类码必须在 enum 中（用 .value 字符串校验，避免被改名后未察觉）。"""
    must_have = {
        "upstream_invalid_request",
        "upstream_rate_limited",
        "upstream_server_error",
        "upstream_auth_error",
        "upstream_timeout",  # 老枚举里已有，新增映射要复用
        "upstream_cancelled",
        "upstream_network_error",
        "upstream_payload_too_large",
        "upstream_context_too_long",
        "upstream_unknown",
    }
    actual = {c.value for c in GenerationErrorCode}
    missing = must_have - actual
    assert not missing, f"缺失上游分类码：{missing}"


# --- UPSTREAM_TYPE_TO_CODE 覆盖度 ---


def test_type_map_covers_official_four() -> None:
    """OpenAI-compatible 上游官方约定 4 类 error.type 必须命中。"""
    for t in (
        "invalid_request_error",
        "rate_limit_error",
        "server_error",
        "authentication_error",
    ):
        assert t in UPSTREAM_TYPE_TO_CODE, f"type 缺映射: {t}"


def test_type_map_values_are_enum_members() -> None:
    """避免 dict value 退化成裸字符串导致 retry.py 比较失败。"""
    for value in UPSTREAM_TYPE_TO_CODE.values():
        assert isinstance(value, GenerationErrorCode)
    for value in UPSTREAM_CODE_TO_CODE.values():
        assert isinstance(value, GenerationErrorCode)


# --- classify_upstream_error 优先级 ---


def test_classify_prefers_type_over_status() -> None:
    """type 命中时不应被 status 覆盖（即使 status 看起来更"高优先级"）。"""
    code = classify_upstream_error("invalid_request_error", 500)
    assert code is GenerationErrorCode.UPSTREAM_INVALID_REQUEST


def test_classify_uses_error_code_when_type_missing() -> None:
    """type 缺失时退到 error.code 上的常见语义（context_length_exceeded 等）。"""
    code = classify_upstream_error(None, 400, error_code="context_length_exceeded")
    assert code is GenerationErrorCode.UPSTREAM_CONTEXT_TOO_LONG


def test_classify_falls_back_to_status() -> None:
    cases = [
        (401, GenerationErrorCode.UPSTREAM_AUTH_ERROR),
        (403, GenerationErrorCode.UPSTREAM_AUTH_ERROR),
        (408, GenerationErrorCode.UPSTREAM_TIMEOUT),
        (413, GenerationErrorCode.UPSTREAM_PAYLOAD_TOO_LARGE),
        (429, GenerationErrorCode.UPSTREAM_RATE_LIMITED),
        (500, GenerationErrorCode.UPSTREAM_SERVER_ERROR),
        (502, GenerationErrorCode.UPSTREAM_SERVER_ERROR),
        (504, GenerationErrorCode.UPSTREAM_TIMEOUT),
        (400, GenerationErrorCode.UPSTREAM_INVALID_REQUEST),
        (404, GenerationErrorCode.UPSTREAM_INVALID_REQUEST),
    ]
    for status, expected in cases:
        assert classify_upstream_error(None, status) is expected, status


def test_classify_unknown_when_no_signal() -> None:
    """type / code / status 全空 → UNKNOWN，调用方据此走通用文案。"""
    assert (
        classify_upstream_error(None, None) is GenerationErrorCode.UPSTREAM_UNKNOWN
    )


def test_classify_unknown_for_weird_status() -> None:
    """非 4xx/5xx 区间（例如 100 / 200 / 300）应走 UNKNOWN，不要乱归类。"""
    assert classify_upstream_error(None, 200) is GenerationErrorCode.UPSTREAM_UNKNOWN
    assert classify_upstream_error(None, 302) is GenerationErrorCode.UPSTREAM_UNKNOWN


def test_classify_normalizes_type_case_and_whitespace() -> None:
    """上游可能给大小写不一致的 type；分类层应保持鲁棒。"""
    code = classify_upstream_error("  Rate_Limit_Error  ", None)
    assert code is GenerationErrorCode.UPSTREAM_RATE_LIMITED


def test_classify_unknown_type_with_status_fallback() -> None:
    """type 是没见过的字符串但 status 有意义 → 走 status 兜底，而不是 UNKNOWN。"""
    code = classify_upstream_error("brand_new_error_type", 429)
    assert code is GenerationErrorCode.UPSTREAM_RATE_LIMITED


@pytest.mark.parametrize(
    "etype,expected",
    [
        ("invalid_request_error", GenerationErrorCode.UPSTREAM_INVALID_REQUEST),
        ("rate_limit_error", GenerationErrorCode.UPSTREAM_RATE_LIMITED),
        ("server_error", GenerationErrorCode.UPSTREAM_SERVER_ERROR),
        ("authentication_error", GenerationErrorCode.UPSTREAM_AUTH_ERROR),
        ("permission_error", GenerationErrorCode.UPSTREAM_AUTH_ERROR),
        ("timeout", GenerationErrorCode.UPSTREAM_TIMEOUT),
    ],
)
def test_classify_type_table(
    etype: str, expected: GenerationErrorCode
) -> None:
    assert classify_upstream_error(etype, None) is expected
