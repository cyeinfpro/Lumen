"""apparel-model-library 子模块：常量 + 小型共享 helper。

从 workflows.py 拆出来减少其规模（原 ~4000 行）。仅放无外部依赖的常量和
纯函数，以及同步状态文件使用的跨进程短锁；index/sync/db 相关 helper 仍留在
workflows.py，以避免循环 import。

workflows.py 顶部把这里的 symbol 全部 re-export，使 `workflows._xxx` 仍
可被测试访问。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import os
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator

MODEL_LIBRARY_SCHEMA_VERSION = 1
# 模块级常量都用 frozenset / MappingProxyType 包裹，避免被调用方意外 mutate
MODEL_LIBRARY_SOURCES: frozenset[str] = frozenset(
    {"all", "preset", "favorite", "user_upload", "generated"}
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
# 跨进程同步用可续租 lease，而不是在整个 GitHub I/O 期间持有文件锁。
MODEL_LIBRARY_SYNC_LEASE_SECONDS = 5 * 60
MODEL_LIBRARY_SYNC_LEASE_RENEW_SECONDS = 60
# GitHub Contents 遍历与响应体预算。当前仓库约 1.1k 个文件，这些上限留有
# 足够扩容空间，同时阻断异常目录树、超大 JSON 和无限递归。
MODEL_LIBRARY_MAX_GITHUB_DEPTH = 8
MODEL_LIBRARY_MAX_GITHUB_DIRECTORIES = 128
MODEL_LIBRARY_MAX_GITHUB_FILES = 4096
MODEL_LIBRARY_MAX_GITHUB_RESPONSE_BYTES = 4 * 1024 * 1024
MODEL_LIBRARY_MAX_GITHUB_METADATA_BYTES = 32 * 1024 * 1024
# 库二进制单文件最大字节，防止恶意/损坏 preset 拖垮带宽
MODEL_LIBRARY_MAX_BINARY_BYTES = 50 * 1024 * 1024
# 一次同步的实际下载总量。GitHub SHA 未变化且本地缓存存在时不会计入。
MODEL_LIBRARY_MAX_SYNC_DOWNLOAD_BYTES = 512 * 1024 * 1024
# index / sync-state / legacy user index 的本地 JSON 读取上限。
MODEL_LIBRARY_MAX_INDEX_BYTES = 32 * 1024 * 1024
MODEL_LIBRARY_IMAGE_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp"}
)
MODEL_LIBRARY_GENDER_SEGMENTS: frozenset[str] = frozenset({"female", "male"})
# 外貌偏向枚举：含 "all" 用于 list 接口"不限"语义；db 列允许任一值（不含 all）。
MODEL_LIBRARY_APPEARANCES: frozenset[str] = frozenset(
    {
        "all",
        "asian",
        "east_asian",
        "southeast_asian",
        "south_asian",
        "european",
        "latin",
        "middle_eastern",
        "african",
        "mixed",
        "other",
    }
)
# 中英文 + 别名归整表。key 已 lowercase + 去空格/连字符（统一替换为下划线）。
_APPEARANCE_ALIASES: MappingProxyType[str, str] = MappingProxyType(
    {
        "asian": "asian",
        "亚洲": "asian",
        "亚裔": "asian",
        "east_asian": "east_asian",
        "eastasian": "east_asian",
        "东亚": "east_asian",
        "东亚人": "east_asian",
        "中国": "east_asian",
        "日本": "east_asian",
        "韩国": "east_asian",
        "chinese": "east_asian",
        "japanese": "east_asian",
        "korean": "east_asian",
        "southeast_asian": "southeast_asian",
        "southeastasian": "southeast_asian",
        "东南亚": "southeast_asian",
        "thai": "southeast_asian",
        "vietnamese": "southeast_asian",
        "filipino": "southeast_asian",
        "south_asian": "south_asian",
        "southasian": "south_asian",
        "南亚": "south_asian",
        "印度": "south_asian",
        "indian": "south_asian",
        "european": "european",
        "caucasian": "european",
        "欧洲": "european",
        "白人": "european",
        "西方": "european",
        "latin": "latin",
        "hispanic": "latin",
        "latino": "latin",
        "latina": "latin",
        "拉美": "latin",
        "拉丁": "latin",
        "middle_eastern": "middle_eastern",
        "middleeastern": "middle_eastern",
        "arab": "middle_eastern",
        "中东": "middle_eastern",
        "阿拉伯": "middle_eastern",
        "african": "african",
        "black": "african",
        "非洲": "african",
        "非裔": "african",
        "mixed": "mixed",
        "混血": "mixed",
        "multiracial": "mixed",
        "other": "other",
    }
)
# 模特库独立生成允许的张数档位；前端按钮组也按此白名单。
MODEL_LIBRARY_GENERATE_COUNTS: frozenset[int] = frozenset({1, 2, 4, 16})
# 隐藏 workflow type：模特库独立生成任务，不出现在 ProjectsIndex 列表里。
WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE = "apparel_model_library_generate"
# 该类型 workflow 的 step_key 和 worker action，与 model_candidates 区分。
MODEL_LIBRARY_GENERATE_STEP_KEY = "model_library_generate"
MODEL_LIBRARY_GENERATE_WORKER_ACTION = "model_library_generate"
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
# 只保护同一进程内的短状态临界区；跨进程互斥由下面的 flock + lease 完成。
# 任何网络请求或大文件 I/O 都不得放在这个锁内。
_SYNC_LOCK = asyncio.Lock()


@contextmanager
def _model_library_sync_file_lock(path: Path) -> Iterator[None]:
    """Serialize short sync-state mutations across API worker processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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


def _normalize_appearance(value: Any) -> str:
    """把各种英文/中文/别名归整到 MODEL_LIBRARY_APPEARANCES。无法识别返回空串。"""
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    if not raw:
        return ""
    # 不区分大小写、空格、连字符 / 下划线（中文不受 lower 影响）
    key = re.sub(r"[\s\-_]+", "_", raw.lower()).strip("_")
    if not key:
        return ""
    aliased = _APPEARANCE_ALIASES.get(key)
    if aliased is not None:
        return aliased
    # 已是合法枚举值（去掉 all）则直接返回
    if key in MODEL_LIBRARY_APPEARANCES and key != "all":
        return key
    return "other"


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
        "adult": "熟龄",
        "middle": "中年",
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
    "MODEL_LIBRARY_APPEARANCES",
    "MODEL_LIBRARY_FETCH_TIMEOUT_SECONDS",
    "MODEL_LIBRARY_FOLDER_BY_AGE",
    "MODEL_LIBRARY_GENDER_SEGMENTS",
    "MODEL_LIBRARY_GENERATE_COUNTS",
    "MODEL_LIBRARY_GENERATE_STEP_KEY",
    "MODEL_LIBRARY_GENERATE_WORKER_ACTION",
    "MODEL_LIBRARY_IMAGE_SUFFIXES",
    "MODEL_LIBRARY_MAX_BINARY_BYTES",
    "MODEL_LIBRARY_MAX_GITHUB_DEPTH",
    "MODEL_LIBRARY_MAX_GITHUB_DIRECTORIES",
    "MODEL_LIBRARY_MAX_GITHUB_FILES",
    "MODEL_LIBRARY_MAX_GITHUB_METADATA_BYTES",
    "MODEL_LIBRARY_MAX_GITHUB_RESPONSE_BYTES",
    "MODEL_LIBRARY_MAX_INDEX_BYTES",
    "MODEL_LIBRARY_MAX_SYNC_DOWNLOAD_BYTES",
    "MODEL_LIBRARY_SCHEMA_VERSION",
    "MODEL_LIBRARY_SOURCES",
    "MODEL_LIBRARY_SYNC_COOLDOWN_SECONDS",
    "MODEL_LIBRARY_SYNC_LEASE_RENEW_SECONDS",
    "MODEL_LIBRARY_SYNC_LEASE_SECONDS",
    "MODEL_LIBRARY_SYNC_MODES",
    "MODEL_LIBRARY_SYNC_RETRY_COOLDOWN_SECONDS",
    "WORKFLOW_TYPE_APPAREL_MODEL_LIBRARY_GENERATE",
    "_SYNC_LOCK",
    "_age_segment_from_folder_name",
    "_gender_from_folder_name",
    "_library_item_url",
    "_model_library_sync_file_lock",
    "_model_library_folder_for_age",
    "_normalize_age_segment",
    "_normalize_appearance",
    "_normalize_model_gender",
    "_preset_id_from_path",
    "_title_from_preset_id",
]
