"""Video 提交的准备态与提交投递状态封装。

从 video_generation_parts/submission.py 拆出,保持主文件在 general module
行数上限内。底层持久化实现仍在 submit_delivery_evidence 中,本模块只做
thin wrapper 与提交准备态数据类。
"""

from __future__ import annotations

from dataclasses import dataclass

from lumen_core.model_entities import VideoGeneration

from ...video_submit_cache import CachedSubmitResult
from . import submit_delivery as submit_delivery_evidence


@dataclass(slots=True)
class SubmitPreparation:
    generation: VideoGeneration
    cached_submit: CachedSubmitResult | None


_persisted_submit_delivery_state_impl = (
    submit_delivery_evidence._persisted_submit_delivery_state  # noqa: SLF001
)
_record_submit_delivery_impl = submit_delivery_evidence._record_submit_delivery  # noqa: SLF001
_submit_delivery_state_impl = submit_delivery_evidence._submit_delivery_state  # noqa: SLF001


def persisted_submit_delivery_state(
    generation: VideoGeneration,
) -> str | None:
    return _persisted_submit_delivery_state_impl(generation)


def submit_delivery_state(generation: VideoGeneration) -> str:
    return _submit_delivery_state_impl(generation)


def record_submit_delivery(
    generation: VideoGeneration,
    *,
    state: str,
    reason: str,
    provider_supports_idempotency: bool | None = None,
    error_code: str | None = None,
) -> None:
    _record_submit_delivery_impl(
        generation,
        state=state,
        reason=reason,
        provider_supports_idempotency=provider_supports_idempotency,
        error_code=error_code,
    )
