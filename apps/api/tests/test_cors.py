import unittest

from fastapi.testclient import TestClient

from app.config import settings
from app.main import build_app


class CorsMiddlewareTests(unittest.TestCase):
    def test_preflight_allows_configured_origin(self) -> None:
        original = settings.cors_allow_origins
        settings.cors_allow_origins = "http://198.51.100.10:3000, http://example.com"
        try:
            client = TestClient(build_app())
            response = client.options(
                "/healthz",
                headers={
                    "Origin": "http://198.51.100.10:3000",
                    "Access-Control-Request-Method": "GET",
                },
            )
        finally:
            settings.cors_allow_origins = original

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "http://198.51.100.10:3000",
        )
        self.assertEqual(
            response.headers.get("access-control-allow-credentials"), "true"
        )

    def test_empty_cors_origins_fail_startup(self) -> None:
        original = settings.cors_allow_origins
        settings.cors_allow_origins = ""
        try:
            with self.assertRaises(ValueError):
                build_app()
        finally:
            settings.cors_allow_origins = original


if __name__ == "__main__":
    unittest.main()
