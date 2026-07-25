from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image as PILImage


_CHUNK_SIZE = 256 * 1024


def image_mime_type(image: PILImage.Image) -> str:
    custom_mimetype = getattr(image, "custom_mimetype", None)
    if isinstance(custom_mimetype, str) and custom_mimetype:
        return custom_mimetype.lower()
    image_format = image.format
    if not isinstance(image_format, str):
        return ""
    mime = PILImage.MIME.get(image_format.upper())
    return mime.lower() if isinstance(mime, str) else ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()
