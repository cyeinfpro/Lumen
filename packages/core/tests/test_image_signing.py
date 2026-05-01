"""image_signing.py 的契约测试。

签名机制是"未来可拓展到 CDN 的图片访问凭证"，必须保持：
- 同 secret + 同输入 → 同签名（确定性）
- 不同 secret / image_id / variant / exp 任一项变化 → 验签失败
- 过期 → 验签失败
- 任何异常输入 → verify 返回 False，不抛
"""

from __future__ import annotations

import pytest

from lumen_core.image_signing import (
    ALLOWED_VARIANTS,
    DEFAULT_TTL_SEC,
    ImageSigningError,
    SIG_HEX_LEN,
    build_signed_path,
    compute_image_sig,
    sign_image_url_query,
    verify_image_sig,
)


SECRET = b"test-secret-32-bytes-long-aaaaaaa"
IMG = "01900000-0000-7000-8000-000000000001"


# --- compute_image_sig ---

def test_compute_image_sig_is_deterministic() -> None:
    a = compute_image_sig(IMG, "orig", 1_700_000_000_000, SECRET)
    b = compute_image_sig(IMG, "orig", 1_700_000_000_000, SECRET)
    assert a == b
    assert len(a) == SIG_HEX_LEN


def test_compute_image_sig_changes_with_secret() -> None:
    a = compute_image_sig(IMG, "orig", 1_700_000_000_000, SECRET)
    b = compute_image_sig(IMG, "orig", 1_700_000_000_000, b"different-secret")
    assert a != b


def test_compute_image_sig_changes_with_variant() -> None:
    a = compute_image_sig(IMG, "orig", 1_700_000_000_000, SECRET)
    b = compute_image_sig(IMG, "thumb256", 1_700_000_000_000, SECRET)
    assert a != b


def test_compute_image_sig_rejects_invalid_variant() -> None:
    with pytest.raises(ImageSigningError):
        compute_image_sig(IMG, "huge8k", 1_700_000_000_000, SECRET)


def test_compute_image_sig_rejects_invalid_image_id() -> None:
    with pytest.raises(ImageSigningError):
        compute_image_sig("../etc/passwd", "orig", 1_700_000_000_000, SECRET)
    with pytest.raises(ImageSigningError):
        compute_image_sig("a|b", "orig", 1_700_000_000_000, SECRET)


def test_compute_image_sig_rejects_empty_secret() -> None:
    with pytest.raises(ImageSigningError):
        compute_image_sig(IMG, "orig", 1_700_000_000_000, b"")


# --- sign_image_url_query ---

def test_sign_returns_exp_in_future() -> None:
    now = 1_700_000_000_000
    exp_ms, sig = sign_image_url_query(
        IMG, "orig", SECRET, ttl_sec=3600, now_ms=now
    )
    assert exp_ms == now + 3600 * 1000
    assert len(sig) == SIG_HEX_LEN


def test_sign_default_ttl() -> None:
    now = 1_700_000_000_000
    exp_ms, _ = sign_image_url_query(IMG, "orig", SECRET, now_ms=now)
    assert exp_ms == now + DEFAULT_TTL_SEC * 1000


def test_sign_rejects_zero_ttl() -> None:
    with pytest.raises(ImageSigningError):
        sign_image_url_query(IMG, "orig", SECRET, ttl_sec=0)


def test_sign_rejects_excessive_ttl() -> None:
    with pytest.raises(ImageSigningError):
        sign_image_url_query(IMG, "orig", SECRET, ttl_sec=31 * 24 * 60 * 60)


# --- verify_image_sig ---

def test_verify_accepts_valid_signature() -> None:
    now = 1_700_000_000_000
    exp_ms, sig = sign_image_url_query(
        IMG, "orig", SECRET, ttl_sec=3600, now_ms=now
    )
    assert verify_image_sig(IMG, "orig", exp_ms, sig, SECRET, now_ms=now) is True


def test_verify_rejects_expired() -> None:
    now = 1_700_000_000_000
    exp_ms, sig = sign_image_url_query(
        IMG, "orig", SECRET, ttl_sec=3600, now_ms=now
    )
    later = exp_ms + 1
    assert (
        verify_image_sig(IMG, "orig", exp_ms, sig, SECRET, now_ms=later) is False
    )


def test_verify_rejects_at_exact_exp() -> None:
    """exp_ms 是过期时刻；准确等于该值的请求应被拒（保守）"""
    now = 1_700_000_000_000
    exp_ms, sig = sign_image_url_query(
        IMG, "orig", SECRET, ttl_sec=3600, now_ms=now
    )
    assert (
        verify_image_sig(IMG, "orig", exp_ms, sig, SECRET, now_ms=exp_ms) is False
    )


def test_verify_rejects_tampered_image_id() -> None:
    now = 1_700_000_000_000
    exp_ms, sig = sign_image_url_query(
        IMG, "orig", SECRET, ttl_sec=3600, now_ms=now
    )
    other = "01900000-0000-7000-8000-000000000002"
    assert (
        verify_image_sig(other, "orig", exp_ms, sig, SECRET, now_ms=now) is False
    )


def test_verify_rejects_tampered_variant() -> None:
    now = 1_700_000_000_000
    exp_ms, sig = sign_image_url_query(
        IMG, "orig", SECRET, ttl_sec=3600, now_ms=now
    )
    assert (
        verify_image_sig(IMG, "thumb256", exp_ms, sig, SECRET, now_ms=now) is False
    )


def test_verify_rejects_tampered_exp() -> None:
    now = 1_700_000_000_000
    exp_ms, sig = sign_image_url_query(
        IMG, "orig", SECRET, ttl_sec=3600, now_ms=now
    )
    assert (
        verify_image_sig(IMG, "orig", exp_ms + 1000, sig, SECRET, now_ms=now)
        is False
    )


def test_verify_rejects_wrong_secret() -> None:
    now = 1_700_000_000_000
    exp_ms, sig = sign_image_url_query(
        IMG, "orig", SECRET, ttl_sec=3600, now_ms=now
    )
    assert (
        verify_image_sig(IMG, "orig", exp_ms, sig, b"another-secret", now_ms=now)
        is False
    )


def test_verify_rejects_malformed_inputs() -> None:
    """异常输入应一律返回 False（不抛）。"""
    now = 1_700_000_000_000
    assert verify_image_sig(IMG, "orig", 0, "abc", SECRET, now_ms=now) is False
    assert verify_image_sig(IMG, "orig", now + 1000, "", SECRET, now_ms=now) is False
    assert (
        verify_image_sig(IMG, "orig", now + 1000, "tooshort", SECRET, now_ms=now)
        is False
    )
    assert (
        verify_image_sig(IMG, "../etc", now + 1000, "a" * SIG_HEX_LEN, SECRET, now_ms=now)
        is False
    )


# --- build_signed_path ---

def test_build_signed_path_format() -> None:
    now = 1_700_000_000_000
    path = build_signed_path(IMG, "thumb256", SECRET, ttl_sec=3600, now_ms=now)
    assert path.startswith(f"/api/images/_/sig/{IMG}/thumb256?exp=")
    assert "&sig=" in path


def test_build_signed_path_round_trip_via_verify() -> None:
    """build_signed_path 出来的 URL 应能被 verify_image_sig 通过。"""
    from urllib.parse import parse_qs, urlparse

    now = 1_700_000_000_000
    path = build_signed_path(IMG, "display2048", SECRET, ttl_sec=3600, now_ms=now)
    parsed = urlparse(path)
    qs = parse_qs(parsed.query)
    exp_ms = int(qs["exp"][0])
    sig = qs["sig"][0]
    assert (
        verify_image_sig(IMG, "display2048", exp_ms, sig, SECRET, now_ms=now)
        is True
    )


# --- ALLOWED_VARIANTS 同步 ---

def test_allowed_variants_covers_existing_kinds() -> None:
    """ImageVariant.kind 在 routes/images.py 是 display2048 / preview1024 / thumb256，
    再加 orig 走 Image.storage_key。这套 enum 必须包含全部。"""
    assert "orig" in ALLOWED_VARIANTS
    assert "display2048" in ALLOWED_VARIANTS
    assert "preview1024" in ALLOWED_VARIANTS
    assert "thumb256" in ALLOWED_VARIANTS
