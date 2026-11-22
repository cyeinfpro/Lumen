"""Pure limits, indexing and pricing for the private voice workspace."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

MODEL = "qwen-audio-3.1-tts-plus"
PRICE_MICRO_PER_10K = {"cn-beijing": 1_400_000, "ap-southeast-1": 2_190_000}
MAX_JOB_CHARS = 20_000
TARGET_SEGMENT_CHARS = 400
MAX_SEGMENTS = 100
MAX_SAMPLE_BYTES = 10_000_000
SAMPLE_RATE = 24_000
MAX_OUTPUT_BYTES = 128 * 1024 * 1024
_BREAK = re.compile(r"(?<=[。！？；!?;])|(?<=\n\n)")
_WORD = re.compile(r"[A-Za-z0-9]+(?:[.'’@:_/+,\-][A-Za-z0-9]+)*|.", re.DOTALL)
_KEY = re.compile(r"(?:samples|outputs)/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?\.wav\Z")


def _han(code: int) -> bool:
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x20000 <= code <= 0x2EE5F
        or 0x2F800 <= code <= 0x2FA1F
        or 0x30000 <= code <= 0x323AF
    )


def billable_chars(text: str) -> int:
    return sum(2 if _han(ord(char)) else 1 for char in text)


def estimate_micro(characters: int, region: str) -> int:
    """Round upwards consistently so a positive request never shows zero cost."""
    if characters < 0:
        raise ValueError("negative_characters")
    return (characters * PRICE_MICRO_PER_10K[region] + 9_999) // 10_000


def split_text(text: str, target: int = TARGET_SEGMENT_CHARS) -> list[str]:
    text = text.strip()
    if not text or not 16 <= target <= TARGET_SEGMENT_CHARS:
        raise ValueError("invalid_text_or_target")
    if billable_chars(text) > MAX_JOB_CHARS:
        raise ValueError("text_too_long")
    if any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in text):
        raise ValueError("unsupported_control_character")
    result: list[str] = []
    current: list[str] = []
    chars = 0

    def append() -> None:
        nonlocal chars
        if current:
            value = "".join(current)
            if value.strip():
                result.append(value)
            elif result:
                result[-1] += value
        current.clear()
        chars = 0

    for clause in _BREAK.split(text):
        n = billable_chars(clause)
        if n <= target and chars + n > target:
            append()
        for token in _WORD.findall(clause):
            size = billable_chars(token)
            if size > target:
                raise ValueError("unbreakable_text")
            if chars + size > target:
                append()
            current.append(token)
            chars += size
    append()
    if len(result) > MAX_SEGMENTS or any(billable_chars(part) > target for part in result):
        raise ValueError("too_many_segments")
    return result


def request_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def private_path(root: Path, key: str) -> Path:
    if not _KEY.fullmatch(key):
        raise ValueError("invalid_private_audio_key")
    base = root.resolve()
    path = base.joinpath(*key.split("/")).resolve()
    if not path.is_relative_to(base):
        raise ValueError("invalid_private_audio_path")
    return path
