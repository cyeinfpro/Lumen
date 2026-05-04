"""apparel-model-library 子模块：常量 + 纯 helper。

从 workflows.py 拆出来减少其规模（原 ~4000 行）。仅放无外部依赖的常量和
纯函数；index/sync/db 相关 helper 仍留在 workflows.py，以避免循环 import。

workflows.py 顶部把这里的 symbol 全部 re-export，使 `workflows._xxx` 仍
可被测试访问。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any

MODEL_LIBRARY_SCHEMA_VERSION = 1
# 模块级常量都用 frozenset / MappingProxyType 包裹，避免被调用方意外 mutate
MODEL_LIBRARY_SOURCES: frozenset[str] = frozenset(
    {"all", "preset", "favorite", "user_upload"}
)
MODEL_LIBRARY_AGE_SEGMENTS: frozenset[str] = frozenset(
    {
        "all",
        "user_favorites",
        "toddler",
        "child",
        "teen",
        "young_adult",
        "adult",
        "middle_aged",
        "senior",
    }
)
MODEL_LIBRARY_SYNC_MODES: frozenset[str] = frozenset(
    {"admin_only", "any_authenticated", "disabled"}
)
MODEL_LIBRARY_SYNC_COOLDOWN_SECONDS = 5 * 60
# 失败后短冷却：避免 hammer GitHub，但允许用户在临时网络故障后较快重试
MODEL_LIBRARY_SYNC_RETRY_COOLDOWN_SECONDS = 30
MODEL_LIBRARY_FETCH_TIMEOUT_SECONDS = 30.0
# 库二进制单文件最大字节，防止恶意/损坏 preset 拖垮带宽
MODEL_LIBRARY_MAX_BINARY_BYTES = 50 * 1024 * 1024
MODEL_LIBRARY_IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp"}
)
MODEL_LIBRARY_GENDER_SEGMENTS: frozenset[str] = frozenset({"female", "male"})
MODEL_LIBRARY_FOLDER_BY_AGE: MappingProxyType[str, str] = MappingProxyType(
    {
        "user_favorites": "00_user_favorites",
        "toddler": "01_toddler",
        "child": "02_child",
        "teen": "03_teen",
        "young_adult": "04_young_adult",
        "adult": "05_adult",
        "middle_aged": "06_middle_aged",
        "senior": "07_senior",
    }
)
# 同进程串行 GitHub sync，配合 last_attempt_at/last_success_at 防 TOCTOU
_SYNC_LOCK = asyncio.Lock()


def _normalize_age_segment(value: Any) -> str:
    if isinstance(value, str):
        normalized = _age_segment_from_folder_name(value)
        if normalized is not None:
            return normalized
    return "user_favorites"


def _age_segment_from_folder_name(value: str) -> str | None:
    raw = value.strip()
    for age, folder in MODEL_LIBRARY_FOLDER_BY_AGE.items():
        if raw == folder:
            return age
    normalized = re.sub(r"^\d{1,3}[-_]", "", raw)
    if normalized in MODEL_LIBRARY_AGE_SEGMENTS and normalized != "all":
        return normalized
    alt = normalized.replace("-", "_")
    if alt in MODEL_LIBRARY_AGE_SEGMENTS and alt != "all":
        return alt
    return None


def _normalize_model_gender(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"female", "woman", "girl"}:
            return "female"
        if normalized in {"male", "man", "boy"}:
            return "male"
    return "female"


def _gender_from_folder_name(value: str) -> str | None:
    normalized = value.strip().lower()
    return normalized if normalized in MODEL_LIBRARY_GENDER_SEGMENTS else None


def _model_library_folder_for_age(age_segment: Any, gender: Any = None) -> str:
    age = _normalize_age_segment(age_segment)
    age_folder = MODEL_LIBRARY_FOLDER_BY_AGE.get(
        age, MODEL_LIBRARY_FOLDER_BY_AGE["user_favorites"]
    )
    return f"{age_folder}/{_normalize_model_gender(gender)}"


def _library_item_url(item_id: str, kind: str) -> str:
    return f"/api/workflows/apparel-model-library/items/{item_id}/{kind}"


def _preset_id_from_path(path_value: str) -> str:
    path = Path(path_value)
    stem = path.stem
    if stem.endswith(".thumb"):
        stem = stem[: -len(".thumb")]
    parts = [
        part for part in path.parts if part not in {"assets", "apparel-model-presets"}
    ]
    prefix = next(
        (
            age
            for part in reversed(parts[:-1])
            if (age := _age_segment_from_folder_name(part)) is not None
        ),
        None,
    )
    if prefix is not None:
        dashed_prefix = prefix.replace("_", "-")
        if (
            stem == prefix
            or stem.startswith(f"{prefix}-")
            or stem.startswith(f"{prefix}_")
            or stem.startswith(f"{dashed_prefix}-")
        ):
            return stem
        return f"{prefix}-{stem}"
    return stem


def _title_from_preset_id(preset_id: str) -> str:
    words = [part for part in re.split(r"[-_]+", preset_id) if part]
    age_labels = {
        "user": "用户收藏",
        "favorites": "",
        "toddler": "幼儿",
        "child": "儿童",
        "teen": "青少年",
        "young": "青年",
        "adult": "成年",
        "middle": "中老年",
        "aged": "",
        "senior": "老年",
    }
    gender_labels = {"female": "女性", "male": "男性", "woman": "女性", "man": "男性"}
    labels: list[str] = []
    for word in words:
        labels.append(age_labels.get(word, gender_labels.get(word, word)))
    title = " ".join(part for part in labels if part).strip()
    return title or preset_id


__all__ = [
    "MODEL_LIBRARY_AGE_SEGMENTS",
    "MODEL_LIBRARY_FETCH_TIMEOUT_SECONDS",
    "MODEL_LIBRARY_FOLDER_BY_AGE",
    "MODEL_LIBRARY_GENDER_SEGMENTS",
    "MODEL_LIBRARY_IMAGE_SUFFIXES",
    "MODEL_LIBRARY_MAX_BINARY_BYTES",
    "MODEL_LIBRARY_SCHEMA_VERSION",
    "MODEL_LIBRARY_SOURCES",
    "MODEL_LIBRARY_SYNC_COOLDOWN_SECONDS",
    "MODEL_LIBRARY_SYNC_MODES",
    "MODEL_LIBRARY_SYNC_RETRY_COOLDOWN_SECONDS",
    "_SYNC_LOCK",
    "_age_segment_from_folder_name",
    "_gender_from_folder_name",
    "_library_item_url",
    "_model_library_folder_for_age",
    "_normalize_age_segment",
    "_normalize_model_gender",
    "_preset_id_from_path",
    "_title_from_preset_id",
]
