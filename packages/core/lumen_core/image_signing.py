"""HMAC 签名图片 URL 工具。

设计目标：提供"无需登录、有限期生效"的图片访问凭证。当前 `/api/images/*` 都是
登录态 owner-check；这层在它之外**额外**提供一条签名通道，便于：

- `/share/{token}` 内部嵌入图时给一个可缓存、可外推到 CDN 的短链
- 未来切 S3 / Cloudflare R2 时统一切到对象存储原生预签名（API 不变）
- 其他第三方嵌入（embed widget / OG meta）

签名算法：HMAC-SHA256("img|{image_id}|{variant}|{exp_ms}", secret) 取前 24 hex
（96 bits 防碰撞，足够；过长反而拉长 URL）。

**重要**：签名只授予"按 image_id + variant 取 binary"的能力，不授予增删改。
secret 只在 API 进程内使用；不要写进数据库或日志。
"""

from __future__ import annotations

import hmac
import time
from hashlib import sha256

# 签名 HMAC 截取长度（hex 字符数）。24 hex = 96 bits，防碰撞够用。
SIG_HEX_LEN = 24

# 默认 24h；调用方可缩短（短期分享）或延长（永久 embed，但仍受 secret 轮转影响）。
DEFAULT_TTL_SEC = 24 * 60 * 60

# 允许出现在签名 URL 里的 variant 名。`orig` 走 Image.storage_key；其余走 ImageVariant。
ALLOWED_VARIANTS: frozenset[str] = frozenset(
    {"orig", "display2048", "preview1024", "thumb256"}
)


class ImageSigningError(ValueError):
    """签名 / 校验阶段的输入合法性错误。HTTP 层应映射成 400。"""


def _msg(image_id: str, variant: str, exp_ms: int) -> bytes:
    return f"img|{image_id}|{variant}|{exp_ms}".encode("utf-8")


def _validate_inputs(image_id: str, variant: str) -> None:
    if not image_id or "|" in image_id or "/" in image_id:
        raise ImageSigningError(f"invalid image_id: {image_id!r}")
    if variant not in ALLOWED_VARIANTS:
        raise ImageSigningError(
            f"invalid variant: {variant!r}; allowed={sorted(ALLOWED_VARIANTS)}"
        )


def compute_image_sig(
    image_id: str,
    variant: str,
    exp_ms: int,
    secret: bytes,
) -> str:
    """生成 sig（hex，固定 SIG_HEX_LEN 长度）。

    secret 是 API 进程的对称密钥；从环境变量读取，绝不写日志。
    exp_ms 必须由调用方决定（通常 = now + ttl）。
    """
    if not secret:
        raise ImageSigningError("secret must be non-empty bytes")
    _validate_inputs(image_id, variant)
    mac = hmac.new(secret, _msg(image_id, variant, exp_ms), sha256)
    return mac.hexdigest()[:SIG_HEX_LEN]


def sign_image_url_query(
    image_id: str,
    variant: str,
    secret: bytes,
    *,
    ttl_sec: int = DEFAULT_TTL_SEC,
    now_ms: int | None = None,
) -> tuple[int, str]:
    """生成 (exp_ms, sig)，调用方拼成 ?exp={exp_ms}&sig={sig} 即可。

    ttl_sec 必须 > 0；超出 30 天会被拒绝（防止"永久公链"误用）。
    """
    if ttl_sec <= 0:
        raise ImageSigningError(f"ttl_sec must be positive, got {ttl_sec}")
    if ttl_sec > 30 * 24 * 60 * 60:
        raise ImageSigningError(f"ttl_sec must be <= 30 days, got {ttl_sec}")
    base_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    exp_ms = base_ms + ttl_sec * 1000
    sig = compute_image_sig(image_id, variant, exp_ms, secret)
    return exp_ms, sig


def verify_image_sig(
    image_id: str,
    variant: str,
    exp_ms: int,
    sig: str,
    secret: bytes,
    *,
    now_ms: int | None = None,
) -> bool:
    """验签：返回 True 仅当 sig 来自同一 secret 且未过期。

    用 hmac.compare_digest 防计时攻击；任何输入异常一律返回 False（不抛）。
    """
    try:
        if not secret or not sig or not exp_ms:
            return False
        if len(sig) != SIG_HEX_LEN:
            return False
        cur_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        if exp_ms <= cur_ms:
            return False
        want = compute_image_sig(image_id, variant, exp_ms, secret)
        return hmac.compare_digest(sig, want)
    except (ImageSigningError, ValueError, TypeError):
        return False


def build_signed_path(
    image_id: str,
    variant: str,
    secret: bytes,
    *,
    ttl_sec: int = DEFAULT_TTL_SEC,
    now_ms: int | None = None,
) -> str:
    """便利函数：返回相对路径 `/api/images/_/sig/{image_id}/{variant}?exp=&sig=`。

    与 `apps/api/app/routes/images.py` 的 `/_/sig/` 端点配套。前端拿到这条路径
    可直接 <img src=> 引用，反代会送到 API。
    """
    exp_ms, sig = sign_image_url_query(
        image_id, variant, secret, ttl_sec=ttl_sec, now_ms=now_ms
    )
    return f"/api/images/_/sig/{image_id}/{variant}?exp={exp_ms}&sig={sig}"


__all__ = [
    "ALLOWED_VARIANTS",
    "DEFAULT_TTL_SEC",
    "ImageSigningError",
    "SIG_HEX_LEN",
    "build_signed_path",
    "compute_image_sig",
    "sign_image_url_query",
    "verify_image_sig",
]
