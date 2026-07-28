"""Pure resource-demand contract for image-generation admission."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceDemand:
    pixel_units: int
    reference_units: int
    postprocess_units: int
    external_lane_units: int
    output_units: int

    def __post_init__(self) -> None:
        for field_name in (
            "pixel_units",
            "reference_units",
            "postprocess_units",
            "external_lane_units",
            "output_units",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")

    @property
    def total(self) -> int:
        return (
            self.pixel_units
            + self.reference_units
            + self.postprocess_units
            + self.external_lane_units
            + self.output_units
        )


def pixel_resource_units(pixel_count: int | None) -> int:
    if pixel_count is None:
        return 1
    if pixel_count <= 1_600_000:
        return 1
    if pixel_count <= 4_000_000:
        return 2
    return 4


def generation_resource_demand(
    *,
    pixel_count: int | None,
    reference_count: int = 0,
    action: str | None = None,
    has_mask: bool = False,
    transparent: bool = False,
    output_count: int = 1,
    dual_race: bool = False,
) -> ResourceDemand:
    return ResourceDemand(
        pixel_units=pixel_resource_units(pixel_count),
        reference_units=max(0, int(reference_count)),
        postprocess_units=int(action == "edit" or has_mask) + int(transparent),
        external_lane_units=2 if dual_race else 1,
        output_units=max(1, int(output_count)),
    )


__all__ = [
    "ResourceDemand",
    "generation_resource_demand",
    "pixel_resource_units",
]
