"""Runtime defaults and authentication response presentation."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from lumen_core.model_entities import (
    AuthSession,
    User,
)
from lumen_core.schema_models import (
    RuntimeDefaultsOut,
    UserOut,
)

from .runtime import AuthRuntimeAdapter


class RuntimeDefaultsProvider(Protocol):
    async def load(self) -> RuntimeDefaultsOut: ...

    def safe_defaults(self) -> RuntimeDefaultsOut: ...


class DatabaseRuntimeDefaultsProvider:
    def __init__(self, runtime: AuthRuntimeAdapter, db: AsyncSession) -> None:
        self._runtime = runtime
        self._db = db

    @staticmethod
    def safe_defaults() -> RuntimeDefaultsOut:
        return RuntimeDefaultsOut()

    async def load(self) -> RuntimeDefaultsOut:
        defaults = self.safe_defaults()

        spec = self._runtime.get_spec(self._runtime._GENERATION_FAST_DEFAULT_KEY)
        if spec is not None:
            raw = await self._runtime.get_setting(self._db, spec)
            if raw in {"0", "1"}:
                defaults.fast = raw == "1"
        for nav_key, setting_key in self._runtime._NAV_VISIBILITY_SETTING_KEYS.items():
            nav_spec = self._runtime.get_spec(setting_key)
            if nav_spec is None:
                continue
            raw = await self._runtime.get_setting(self._db, nav_spec)
            if raw in {"0", "1"}:
                setattr(defaults.nav_visibility, nav_key, raw == "1")
        canvas_spec = self._runtime.get_spec(self._runtime._CANVAS_ENABLED_KEY)
        if canvas_spec is not None:
            defaults.canvas_enabled = (
                await self._runtime.get_setting(self._db, canvas_spec) == "1"
            )
        return defaults


def user_out_snapshot(user: User) -> UserOut:
    notification_email = getattr(user, "notification_email", None)
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        account_mode=getattr(user, "account_mode", None) or "wallet",
        notification_email=(
            True if notification_email is None else bool(notification_email)
        ),
        default_system_prompt_id=getattr(user, "default_system_prompt_id", None),
    )


def auth_response_snapshot(
    runtime: AuthRuntimeAdapter,
    user: User,
    session: AuthSession,
) -> tuple[str, UserOut]:
    return runtime.generate_csrf_token(session.id), runtime._user_out_snapshot(user)
