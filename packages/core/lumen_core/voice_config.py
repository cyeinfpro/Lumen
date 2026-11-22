"""Atomic, encrypted Voice provider settings shared by the API and Worker."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .byok import ByokCryptoError, decrypt_api_key, encrypt_api_key
from .models import SystemSetting

# Intentionally not a supported general system setting: its ciphertext must not
# appear in the generic configuration or audit APIs.
_VOICE_SETTING_KEY = "voice.provider.private.v1"
VOICE_WORKER_READY_KEY = "lumen:speech:worker-ready:v2"
DEFAULT_VOICE_REGION = "cn-beijing"
VOICE_REGIONS = frozenset({DEFAULT_VOICE_REGION, "ap-southeast-1"})
_WORKSPACE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_KEY = re.compile(r"[!-~]{8,512}")
_REVISION = re.compile(r"[a-f0-9]{32}")


class VoiceProviderConfigurationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class VoiceProviderConfig:
    region: str
    workspace_id: str
    revision: str
    configured: bool
    api_key: str | None = field(default=None, repr=False, compare=False)

    @property
    def ready_marker(self) -> str:
        return f"{self.region}:{self.workspace_id}:{self.revision}"


_DEFAULT = VoiceProviderConfig(DEFAULT_VOICE_REGION, "", "", False)


def _encode(config: VoiceProviderConfig, ciphertext: str | None) -> str:
    return json.dumps(
        {"region": config.region, "workspace_id": config.workspace_id,
         "revision": config.revision, "ciphertext": ciphertext},
        separators=(",", ":"), sort_keys=True,
    )


def _decode(raw: str | None) -> tuple[VoiceProviderConfig, str | None]:
    if raw is None:
        return _DEFAULT, None
    try:
        value = json.loads(raw)
        region, workspace = value["region"], value["workspace_id"]
        revision, ciphertext = value["revision"], value["ciphertext"]
        if (
            region not in VOICE_REGIONS
            or not isinstance(workspace, str)
            or (workspace and not _WORKSPACE.fullmatch(workspace))
            or not isinstance(revision, str)
            or (revision and not _REVISION.fullmatch(revision))
            or (ciphertext is not None and (
                not isinstance(ciphertext, str) or not ciphertext or not revision
            ))
        ):
            raise ValueError("invalid persisted metadata")
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise VoiceProviderConfigurationError("VOICE_PROVIDER_CONFIG_INVALID") from exc
    return VoiceProviderConfig(region, workspace, revision, ciphertext is not None), ciphertext


def _derived_secret(config: VoiceProviderConfig, master_secret: str | None) -> str:
    secret = (master_secret if master_secret is not None else
              os.getenv("BYOK_API_KEY_MASTER_SECRET", "")).strip()
    if len(secret) < 32:
        raise VoiceProviderConfigurationError("VOICE_PROVIDER_CRYPTO_UNAVAILABLE")
    purpose = f"lumen-voice-provider:v1:{config.region}:{config.workspace_id}:{config.revision}"
    return hmac.new(secret.encode(), purpose.encode(), hashlib.sha256).hexdigest()


async def get_voice_provider_state(db: AsyncSession) -> VoiceProviderConfig:
    raw = await db.scalar(select(SystemSetting.value).where(SystemSetting.key == _VOICE_SETTING_KEY))
    return _decode(raw)[0]


async def load_voice_provider_config(
    db: AsyncSession, *, include_key: bool = False, master_secret: str | None = None,
) -> VoiceProviderConfig | None:
    raw = await db.scalar(select(SystemSetting.value).where(SystemSetting.key == _VOICE_SETTING_KEY))
    config, ciphertext = _decode(raw)
    if ciphertext is None:
        return None
    if not include_key:
        return config
    try:
        plaintext = decrypt_api_key(ciphertext, _derived_secret(config, master_secret))
    except ByokCryptoError as exc:
        raise VoiceProviderConfigurationError("VOICE_PROVIDER_CRYPTO_UNAVAILABLE") from exc
    if not _KEY.fullmatch(plaintext):
        raise VoiceProviderConfigurationError("VOICE_PROVIDER_CONFIG_INVALID")
    return VoiceProviderConfig(config.region, config.workspace_id, config.revision, True, plaintext)


async def change_voice_provider_config(
    db: AsyncSession,
    *,
    region: str | None = None,
    workspace_id: str | None = None,
    api_key: str | None = None,
    remove_key: bool = False,
    master_secret: str | None = None,
) -> VoiceProviderConfig:
    """Mutate the single config in the caller's transaction, alongside audit."""
    if region is not None and region not in VOICE_REGIONS:
        raise VoiceProviderConfigurationError("VOICE_PROVIDER_INVALID_REGION")
    if workspace_id is not None:
        workspace_id = workspace_id.strip().lower()
        if workspace_id and not _WORKSPACE.fullmatch(workspace_id):
            raise VoiceProviderConfigurationError("VOICE_PROVIDER_INVALID_WORKSPACE")
    if api_key is not None:
        api_key = api_key.strip()
        if not _KEY.fullmatch(api_key):
            raise VoiceProviderConfigurationError("VOICE_PROVIDER_INVALID_KEY")
    if remove_key and (api_key is not None or region is not None or workspace_id is not None):
        raise VoiceProviderConfigurationError("VOICE_PROVIDER_INVALID_REQUEST")

    # Seed missing rows without a race, then serialize updates on the same row.
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    await db.execute(insert(SystemSetting).values(
        key=_VOICE_SETTING_KEY, value=_encode(_DEFAULT, None),
    ).on_conflict_do_nothing(index_elements=[SystemSetting.key]))
    row = (await db.execute(select(SystemSetting).where(
        SystemSetting.key == _VOICE_SETTING_KEY,
    ).with_for_update())).scalar_one()
    current, old_ciphertext = _decode(row.value)
    region = current.region if region is None else region
    workspace_id = current.workspace_id if workspace_id is None else workspace_id
    scope_changed = (region, workspace_id) != (current.region, current.workspace_id)
    if old_ciphertext and scope_changed and api_key is None:
        raise VoiceProviderConfigurationError("VOICE_PROVIDER_KEY_REQUIRED")
    if not scope_changed and api_key is None and (not remove_key or old_ciphertext is None):
        return current

    config = VoiceProviderConfig(region, workspace_id, uuid.uuid4().hex, api_key is not None)
    try:
        ciphertext = (
            encrypt_api_key(api_key, _derived_secret(config, master_secret))
            if api_key is not None else None
        )
    except ByokCryptoError as exc:
        raise VoiceProviderConfigurationError("VOICE_PROVIDER_CRYPTO_UNAVAILABLE") from exc
    row.value = _encode(config, ciphertext)
    return config
