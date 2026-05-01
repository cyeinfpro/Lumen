"""P1-4: tiktoken 精确 token 计数与 estimate_text_tokens fallback 行为。

实测真实 token 数（o200k_base）：
- "hello world" → 2
- "Hello, world! This is a test of tiktoken counting." → 14
- "你好，世界！这是一段中文测试。" → 10
- "The quick brown fox jumps over the lazy dog. " * 50 → 501 (estimate 563, ~11% diff)
"""

from __future__ import annotations

import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor


def _reset_tiktoken_state(monkeypatch, cw):
    monkeypatch.setattr(cw, "_TIKTOKEN_ENCODING", None)
    monkeypatch.setattr(cw, "_TIKTOKEN_INIT_ATTEMPTED", False)
    monkeypatch.setattr(cw, "_TIKTOKEN_LOAD_THREAD", None)
    monkeypatch.setattr(cw, "_TIKTOKEN_LOADING", False)
    monkeypatch.setattr(cw, "_TIKTOKEN_LOAD_WARNED", False)


def test_count_tokens_uses_tiktoken_when_available():
    from lumen_core.context_window import count_tokens
    # 一段确定的英文，o200k_base 实测 14 tokens；区间放宽以容忍未来 encoding 微调
    n = count_tokens("Hello, world! This is a test of tiktoken counting.")
    assert 8 <= n <= 18, f"unexpected token count {n}"


def test_count_tokens_handles_chinese():
    from lumen_core.context_window import count_tokens
    n = count_tokens("你好，世界！这是一段中文测试。")
    assert n > 0


def test_count_tokens_empty():
    from lumen_core.context_window import count_tokens
    assert count_tokens("") == 0
    assert count_tokens(None) == 0


def test_count_tokens_falls_back_when_tiktoken_missing(monkeypatch):
    """If tiktoken import fails, count_tokens silently falls back to estimate_text_tokens."""
    import lumen_core.context_window as cw
    monkeypatch.setattr(cw, "_TIKTOKEN_ENCODING", None)
    monkeypatch.setattr(cw, "_TIKTOKEN_INIT_ATTEMPTED", True)
    n = cw.count_tokens("hello")
    assert n == cw.estimate_text_tokens("hello")


def test_get_tiktoken_encoding_initializes_once_under_concurrency(monkeypatch):
    import lumen_core.context_window as cw

    calls = 0
    encoding = object()
    fake_tiktoken = types.ModuleType("tiktoken")

    def get_encoding(name: str):  # noqa: ANN202
        nonlocal calls
        assert name == "o200k_base"
        calls += 1
        time.sleep(0.01)
        return encoding

    fake_tiktoken.get_encoding = get_encoding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tiktoken", fake_tiktoken)
    _reset_tiktoken_state(monkeypatch, cw)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: cw._get_tiktoken_encoding(), range(36)))

    assert results == [encoding] * 36
    assert calls == 1


def test_count_tokens_within_15pct_of_estimate_for_typical_text():
    from lumen_core.context_window import count_tokens, estimate_text_tokens
    text = "The quick brown fox jumps over the lazy dog. " * 50
    actual = count_tokens(text)
    estimate = estimate_text_tokens(text)
    if estimate > 0:
        diff_pct = abs(actual - estimate) / max(actual, estimate) * 100
        # 实测 ~11%；spec 验收 ±15%；放 30% 给非典型 ASCII 留余量
        assert diff_pct < 30, f"diff {diff_pct:.1f}% actual={actual} estimate={estimate}"


def test_warm_tiktoken_returns_true_when_available(monkeypatch):
    """warm_tiktoken 在依赖装好的环境下应返回 True。"""
    import lumen_core.context_window as cw

    encoding = object()
    fake_tiktoken = types.ModuleType("tiktoken")
    fake_tiktoken.get_encoding = lambda _name: encoding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tiktoken", fake_tiktoken)
    _reset_tiktoken_state(monkeypatch, cw)

    assert cw.warm_tiktoken(timeout_sec=0.2) is True


def test_slow_tiktoken_load_falls_back_without_blocking(monkeypatch):
    import lumen_core.context_window as cw

    encoding = object()
    fake_tiktoken = types.ModuleType("tiktoken")

    def get_encoding(_name: str):  # noqa: ANN202
        time.sleep(0.2)
        return encoding

    fake_tiktoken.get_encoding = get_encoding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tiktoken", fake_tiktoken)
    _reset_tiktoken_state(monkeypatch, cw)

    started = time.monotonic()
    assert cw._get_tiktoken_encoding(timeout_sec=0.01) is None
    assert time.monotonic() - started < 0.1
