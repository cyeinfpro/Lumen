"""Public image-artifact contract consumed by generation runtime parts.

The legacy artifact implementation remains an internal worker detail.  This
module is the single, explicit boundary exposed to generation orchestration.
"""

from __future__ import annotations

from ... import image_artifacts as _implementation

ALLOWED_UPSTREAM_IMAGE_FORMATS = _implementation._ALLOWED_UPSTREAM_IMAGE_FORMATS
GeneratedImageInspection = _implementation._GeneratedImageInspection
ImageVariantBundle = _implementation._ImageVariantBundle
MAX_UPSTREAM_IMAGE_SIDE = _implementation._MAX_UPSTREAM_IMAGE_SIDE
PostprocessedGeneratedImage = _implementation._PostprocessedGeneratedImage
VariantPayload = _implementation._VariantPayload
compute_blurhash = _implementation._compute_blurhash
decode_upstream_image_b64 = _implementation._decode_upstream_image_b64
image_has_alpha = _implementation._image_has_alpha
image_has_transparency = _implementation._image_has_transparency
inspect_generated_image_sync = _implementation._inspect_generated_image_sync
make_display = _implementation._make_display
make_preview = _implementation._make_preview
make_thumb = _implementation._make_thumb
make_variants_with_pil_sync = _implementation._make_variants_with_pil_sync
make_variants_with_vips_sync = _implementation._make_variants_with_vips_sync
resize_vips_image = _implementation._resize_vips_image
rgb_image_for_flat_variant = _implementation._rgb_image_for_flat_variant
sha256 = _implementation._sha256
validate_generated_image_metadata = _implementation._validate_generated_image_metadata
webp_image_for_variant = _implementation._webp_image_for_variant

__all__ = [
    "ALLOWED_UPSTREAM_IMAGE_FORMATS",
    "GeneratedImageInspection",
    "ImageVariantBundle",
    "MAX_UPSTREAM_IMAGE_SIDE",
    "PostprocessedGeneratedImage",
    "VariantPayload",
    "compute_blurhash",
    "decode_upstream_image_b64",
    "image_has_alpha",
    "image_has_transparency",
    "inspect_generated_image_sync",
    "make_display",
    "make_preview",
    "make_thumb",
    "make_variants_with_pil_sync",
    "make_variants_with_vips_sync",
    "resize_vips_image",
    "rgb_image_for_flat_variant",
    "sha256",
    "validate_generated_image_metadata",
    "webp_image_for_variant",
]
