from __future__ import annotations

from pathlib import Path

from app.images.application import http_routes


API_ROOT = Path(__file__).resolve().parents[2]
ROUTE_FILE = API_ROOT / "app" / "routes" / "images.py"


def test_images_route_compatibility_facade_is_removed() -> None:
    assert not ROUTE_FILE.exists()


def test_images_router_is_owned_by_the_images_application_boundary() -> None:
    assert http_routes.__name__ == "app.images.application.http_routes"
