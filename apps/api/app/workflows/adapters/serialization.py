from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PIL import Image as PILImage
from lumen_core.models import Image

from ...config import settings
from ..application.errors import WorkflowRequestError


logger = logging.getLogger("app.routes.workflows")

_SHOWCASE_GPT55_REFERENCE_MAX_BYTES = 900_000


def _http(
    code: str,
    msg: str,
    http: int = 400,
    **extra: Any,
) -> WorkflowRequestError:
    return WorkflowRequestError(
        status_code=http,
        code=code,
        message=msg,
        details=extra or None,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dedupe_nonempty(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def _accessory_preview_request_key(
    *,
    candidate_id: str,
    accessory_plan: dict[str, Any],
    style_prompt: str,
) -> str:
    payload = {
        "candidate_id": candidate_id,
        "accessory_plan": accessory_plan,
        "style_prompt": style_prompt.strip(),
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _clean_optional_text(
    value: str | None,
    *,
    max_len: int = 120,
) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def _clean_style_tags(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        tag = raw.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag[:32])
        if len(out) >= 12:
            break
    return out


def _clean_string_list(
    values: Iterable[str],
    *,
    max_items: int,
    max_len: int,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        item = raw.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item[:max_len])
        if len(out) >= max_items:
            break
    return out


def _safe_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _iso_now() -> str:
    return _now().isoformat().replace("+00:00", "Z")


def _storage_root() -> Path:
    return Path(settings.storage_root).resolve()


def _storage_path(storage_key: str) -> Path:
    root = _storage_root()
    if not storage_key or "\x00" in storage_key:
        raise _http("invalid_path", "invalid storage path", 400)
    key_path = Path(storage_key)
    if key_path.is_absolute():
        raise _http("invalid_path", "absolute storage paths are not allowed", 400)
    path = (root / key_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise _http("invalid_path", "storage path escapes root", 400)
    return path


def _showcase_gpt55_reference_data_url(image: Image, raw: bytes) -> str | None:
    if not raw:
        return None
    try:
        with PILImage.open(io.BytesIO(raw)) as original:
            original.load()
            last_payload: bytes | None = None
            for max_side, quality in ((1280, 88), (1024, 82), (768, 76)):
                with original.copy() as im:
                    im.thumbnail((max_side, max_side))
                    if im.mode in {"RGBA", "LA"} or "transparency" in im.info:
                        rgba = im.convert("RGBA")
                        rgb = PILImage.new("RGB", rgba.size, (255, 255, 255))
                        rgb.paste(rgba, mask=rgba.getchannel("A"))
                        rgba.close()
                    else:
                        rgb = im.convert("RGB")
                    buf = io.BytesIO()
                    with rgb:
                        rgb.save(buf, format="JPEG", quality=quality, optimize=True)
                    last_payload = buf.getvalue()
                    if len(last_payload) <= _SHOWCASE_GPT55_REFERENCE_MAX_BYTES:
                        b64 = base64.b64encode(last_payload).decode("ascii")
                        return f"data:image/jpeg;base64,{b64}"
            logger.info(
                "showcase gpt55 reference too large after downsample image_id=%s bytes=%s",
                getattr(image, "id", ""),
                len(last_payload or b""),
            )
            return None
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "showcase gpt55 reference downsample failed image_id=%s err=%s",
            getattr(image, "id", ""),
            exc,
        )
        return None


# Public workflow contracts.
SHOWCASE_GPT55_REFERENCE_MAX_BYTES = _SHOWCASE_GPT55_REFERENCE_MAX_BYTES
accessory_preview_request_key = _accessory_preview_request_key
clean_optional_text = _clean_optional_text
clean_string_list = _clean_string_list
clean_style_tags = _clean_style_tags
dedupe_nonempty = _dedupe_nonempty
dict_or_empty = _dict_or_empty
http = _http
iso_now = _iso_now
now = _now
safe_datetime = _safe_datetime
showcase_gpt55_reference_data_url = _showcase_gpt55_reference_data_url
storage_path = _storage_path
storage_root = _storage_root
