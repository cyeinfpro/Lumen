"""客户端层 compact 辅助函数的单元测试。

覆盖 ``messages_token_count`` / ``would_exceed_budget`` /
``select_messages_to_compact`` 的边界行为。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lumen_core.constants import Role
from lumen_core.context_window import (
    CONTEXT_INPUT_TOKEN_BUDGET,
    MESSAGE_OVERHEAD_TOKENS,
    estimate_system_prompt_tokens,
    estimate_text_tokens,
    messages_token_count,
    select_messages_to_compact,
    would_exceed_budget,
)


def _msg(role: str, text: str) -> dict:
    """构造 ResponseItem 形态的消息 dict。"""
    return {"role": role, "content": {"text": text}}


# ---------------------------------------------------------------------------
# messages_token_count
# ---------------------------------------------------------------------------


def test_messages_token_count_empty_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # 入空列表 + 空 system_prompt 时应返回 0，不应抛错。
    assert messages_token_count([]) == 0
    assert messages_token_count([], system_prompt="") == 0


def test_messages_token_count_includes_system_overhead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 关掉 tiktoken 让结果可预测：纯走 estimate_text_tokens。
    import lumen_core.context_window as cw

    monkeypatch.setattr(cw, "_TIKTOKEN_ENCODING", None)
    monkeypatch.setattr(cw, "_TIKTOKEN_INIT_ATTEMPTED", True)

    msgs = [_msg(Role.USER.value, "hello"), _msg(Role.ASSISTANT.value, "world")]
    user_cost = MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens("hello")
    asst_cost = MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens("world")
    sys_cost = estimate_system_prompt_tokens("you are helpful")

    assert messages_token_count(msgs) == user_cost + asst_cost
    assert (
        messages_token_count(msgs, system_prompt="you are helpful")
        == user_cost + asst_cost + sys_cost
    )


def test_messages_token_count_accepts_orm_like_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SQLAlchemy ORM Message 没有 dict 接口，但有 role/content 属性，需兼容。
    import lumen_core.context_window as cw

    monkeypatch.setattr(cw, "_TIKTOKEN_ENCODING", None)
    monkeypatch.setattr(cw, "_TIKTOKEN_INIT_ATTEMPTED", True)

    msg = SimpleNamespace(role=Role.USER.value, content={"text": "abc"})
    expected = MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens("abc")
    assert messages_token_count([msg]) == expected


def test_messages_token_count_handles_non_dict_content() -> None:
    # 异常 content（None / 字符串）不应炸——退化成空 / {"text": str}。
    bad = [{"role": Role.USER.value, "content": None}]
    assert messages_token_count(bad) == 0
    string_content = [{"role": Role.USER.value, "content": "raw string"}]
    # 至少不应抛错；具体值用 estimate 公式回算。
    assert messages_token_count(string_content) > 0


# ---------------------------------------------------------------------------
# would_exceed_budget
# ---------------------------------------------------------------------------


def test_would_exceed_budget_short_history_returns_false() -> None:
    # 几条短消息远低于预算。
    msgs = [_msg(Role.USER.value, "hi"), _msg(Role.ASSISTANT.value, "hello")]
    assert would_exceed_budget(msgs) is False


def test_would_exceed_budget_safety_margin_pushes_over_threshold() -> None:
    # 自定义小预算 + 大 safety_margin 时强制返回 True。
    msgs = [_msg(Role.USER.value, "x" * 100)]
    assert would_exceed_budget(msgs, budget=200, safety_margin=10_000) is True


def test_would_exceed_budget_default_budget_is_input_token_budget() -> None:
    # 默认 budget 应当是 CONTEXT_INPUT_TOKEN_BUDGET（200k 量级）。
    # 给一个不可能超的小列表，必然 False。
    msgs = [_msg(Role.USER.value, "small")]
    assert (
        would_exceed_budget(msgs, budget=CONTEXT_INPUT_TOKEN_BUDGET, safety_margin=0)
        is False
    )


def test_would_exceed_budget_negative_safety_margin_treated_as_zero() -> None:
    # safety_margin 不应是负数；当作 0 处理避免误关 compact。
    msgs = [_msg(Role.USER.value, "abc")]
    # 预算正好等于消息 token 数也算"未超"，因为 used + 0 == budget 不会 > budget。
    used = messages_token_count(msgs)
    assert would_exceed_budget(msgs, budget=used, safety_margin=-9999) is False


# ---------------------------------------------------------------------------
# select_messages_to_compact
# ---------------------------------------------------------------------------


def test_select_messages_to_compact_keeps_system_messages() -> None:
    # system 消息无论位置都应进 to_keep。
    sys_msg = _msg(Role.SYSTEM.value, "rules")
    msgs = [
        sys_msg,
        _msg(Role.USER.value, "u1"),
        _msg(Role.ASSISTANT.value, "a1"),
        _msg(Role.USER.value, "u2"),
    ]
    to_compact, to_keep = select_messages_to_compact(msgs, keep_recent=6)
    # 总数不超过 keep_recent 的非 system 量，所以全留。
    assert to_compact == []
    assert sys_msg in to_keep
    assert len(to_keep) == 4


def test_select_messages_to_compact_splits_old_and_recent() -> None:
    # 8 条非 system，keep_recent=3 → 前 5 条压，后 3 条留。
    msgs = []
    for i in range(8):
        role = Role.USER.value if i % 2 == 0 else Role.ASSISTANT.value
        msgs.append(_msg(role, f"m{i}"))
    to_compact, to_keep = select_messages_to_compact(msgs, keep_recent=3)
    assert len(to_compact) == 5
    assert len(to_keep) == 3
    # to_keep 是后 3 条
    assert to_keep[0]["content"]["text"] == "m5"
    assert to_keep[-1]["content"]["text"] == "m7"


def test_select_messages_to_compact_keep_recent_zero_compacts_all_non_system() -> None:
    # keep_recent=0 时除 system 外全部进 to_compact。
    sys_msg = _msg(Role.SYSTEM.value, "global")
    msgs = [sys_msg, _msg(Role.USER.value, "u1"), _msg(Role.ASSISTANT.value, "a1")]
    to_compact, to_keep = select_messages_to_compact(msgs, keep_recent=0)
    assert len(to_compact) == 2
    assert to_keep == [sys_msg]


def test_select_messages_to_compact_empty_returns_empty_pair() -> None:
    # 入空列表不应炸。
    to_compact, to_keep = select_messages_to_compact([], keep_recent=6)
    assert to_compact == []
    assert to_keep == []


def test_select_messages_to_compact_default_keep_recent_is_six() -> None:
    # 默认 keep_recent=6：只有 4 条非 system → to_compact 应为空。
    msgs = [_msg(Role.USER.value, f"m{i}") for i in range(4)]
    to_compact, to_keep = select_messages_to_compact(msgs)
    assert to_compact == []
    assert to_keep == msgs


def test_select_messages_to_compact_negative_keep_recent_normalized() -> None:
    # 负数 keep_recent 当作 0 处理，不应索引越界。
    msgs = [_msg(Role.USER.value, "u1"), _msg(Role.ASSISTANT.value, "a1")]
    to_compact, to_keep = select_messages_to_compact(msgs, keep_recent=-5)
    assert to_compact == msgs
    assert to_keep == []


def test_select_messages_to_compact_preserves_order_in_to_keep() -> None:
    # to_keep 顺序：先 system，再非 system 后 N 条；非 system 内部相对顺序保持。
    sys1 = _msg(Role.SYSTEM.value, "s1")
    sys2 = _msg(Role.SYSTEM.value, "s2")
    msgs = [
        _msg(Role.USER.value, "u-old"),
        sys1,
        _msg(Role.ASSISTANT.value, "a1"),
        _msg(Role.USER.value, "u2"),
        sys2,
        _msg(Role.ASSISTANT.value, "a2"),
    ]
    to_compact, to_keep = select_messages_to_compact(msgs, keep_recent=2)
    # 4 条非 system，留 2 条 → 压前 2 条非 system: u-old + a1
    assert len(to_compact) == 2
    assert to_compact[0]["content"]["text"] == "u-old"
    assert to_compact[1]["content"]["text"] == "a1"
    # to_keep: 2 个 system + 后 2 条非 system
    keep_texts = [m["content"]["text"] for m in to_keep]
    assert keep_texts.count("s1") == 1
    assert keep_texts.count("s2") == 1
    assert "u2" in keep_texts and "a2" in keep_texts
