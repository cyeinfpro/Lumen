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
