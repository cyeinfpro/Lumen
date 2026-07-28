from __future__ import annotations

from pathlib import Path

from app.images.application import http_routes
from app.routes import images


API_ROOT = Path(__file__).resolve().parents[2]
ROUTE_FILE = API_ROOT / "app" / "routes" / "images.py"


def test_images_route_aggregation_stays_below_250_lines() -> None:
    assert len(ROUTE_FILE.read_text(encoding="utf-8").splitlines()) < 250


def test_images_router_is_owned_by_the_images_application_boundary() -> None:
    assert images.router is http_routes.router
    assert http_routes.__name__ == "app.images.application.http_routes"


def test_images_route_facade_declares_its_supported_public_api() -> None:
    assert images.__all__ == [
        "ALLOWED_MIME",
        "ALLOWED_VARIANTS",
        "DISPLAY_VARIANT",
        "EXT_BY_MIME",
        "MAX_BYTES",
        "MAX_IMAGE_PIXELS",
        "MAX_LONG_SIDE",
        "NORMALIZABLE_UPLOAD_MIME",
        "PILImage",
        "UPLOADS_LIMITER",
        "VARIANT_MEDIA_TYPE",
        "VOLCANO_ASSET_UPLOAD_MAX_LONG_SIDE",
        "delete_image",
        "get_image_binary",
        "get_image_by_key",
        "get_image_meta",
        "get_image_signed",
        "get_image_variant",
        "os",
        "reference_image_binary",
        "reference_image_binary_named",
        "router",
        "settings",
        "shutil",
        "sweep_orphan_image_files",
        "upload_image",
        "upload_image_impl",
    ]
