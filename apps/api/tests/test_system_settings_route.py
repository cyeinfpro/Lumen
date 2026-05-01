from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Request

from app.routes import system_settings
from lumen_core.schemas import SystemSettingsUpdateIn


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/admin/settings",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


@pytest.mark.asyncio
async def test_put_settings_rejects_empty_string_for_typed_setting() -> None:
    with pytest.raises(Exception) as excinfo:
        await system_settings.put_settings_endpoint(
            SystemSettingsUpdateIn(
                items=[{"key": "context.summary_target_tokens", "value": ""}]
            ),
            _request(),
            SimpleNamespace(id="admin-1", email="admin@example.com"),
            object(),  # type: ignore[arg-type]
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert excinfo.value.detail["error"]["details"]["errors"][0]["key"] == (
        "context.summary_target_tokens"
    )
