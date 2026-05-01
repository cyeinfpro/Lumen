from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from PIL import Image as PILImage


@dataclass
class BackgroundRemovalResult:
    rgba: PILImage.Image
    alpha_mask: PILImage.Image
    provider: str
    confidence: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def close(self) -> None:
        for im in (self.rgba, self.alpha_mask):
            try:
                im.close()
            except Exception:
                pass


class BackgroundRemovalProvider(Protocol):
    name: str

    async def remove_background(
        self,
        image: PILImage.Image,
        *,
        prompt: str | None = None,
    ) -> BackgroundRemovalResult | None:
        ...


@dataclass
class TransparentQcReport:
    passed: bool
    score: float
    failure_reasons: list[str]
    warnings: list[str]
    foreground_bbox: tuple[int, int, int, int] | None
    alpha_coverage: float
    border_alpha_max: int
    largest_component_ratio: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "score": round(self.score, 4),
            "failure_reasons": list(self.failure_reasons),
            "warnings": list(self.warnings),
            "foreground_bbox": list(self.foreground_bbox) if self.foreground_bbox else None,
            "alpha_coverage": round(self.alpha_coverage, 4),
            "border_alpha_max": self.border_alpha_max,
            "largest_component_ratio": (
                round(self.largest_component_ratio, 4)
                if self.largest_component_ratio is not None
                else None
            ),
        }


@dataclass
class TransparentPipelineOutput:
    rgba_png: bytes
    alpha_mask_png: bytes
    width: int
    height: int
    provider: str
    qc: TransparentQcReport


class TransparentPipelineFailure(Exception):
    def __init__(self, message: str, *, qc: TransparentQcReport | None, provider: str | None) -> None:
        super().__init__(message)
        self.qc = qc
        self.provider = provider
