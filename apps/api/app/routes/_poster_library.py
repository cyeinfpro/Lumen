"""poster-style-library 子模块：常量 + 纯 helper。

模特库的"视觉风格"孪生版。设计上严格对齐 ``_apparel_library``，差异如下：

* 元数据从 ``meta.json`` 解析（``prompt_template`` 长度无法塞文件名）。
* 每条 PosterStyleItem 可以有 N 张 sample 图，cover_image_id = sample[0]。
* 没有 age_segment / gender / appearance_direction，改用 category / mood / palette。

模块只放无外部依赖的常量与纯函数；index/sync/db helper 留在 ``poster_styles.py``，
避免循环 import（同 apparel 一致）。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import json
import logging
import os
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator

from lumen_core.constants import (
    POSTER_STYLE_CATEGORIES,
    POSTER_STYLE_DEFAULT_ASPECTS,
    POSTER_STYLE_FETCH_TIMEOUT_S,
    POSTER_STYLE_IMAGE_SUFFIXES,
    POSTER_STYLE_LIBRARY_FOLDER,
    POSTER_STYLE_MAX_BINARY_BYTES,
    POSTER_STYLE_MAX_SAMPLES,
    POSTER_STYLE_PRESET_ROOT,
    POSTER_STYLE_SCHEMA_VERSION,
    POSTER_STYLE_SOURCES,
    POSTER_STYLE_SYNC_COOLDOWN_S,
    POSTER_STYLE_SYNC_FAILURE_COOLDOWN_S,
    POSTER_STYLE_SYNC_MODES,
)

logger = logging.getLogger(__name__)

# --- 路由 / Workflow 常量 ----------------------------------------------------
# 隐藏 workflow type：风格库独立生成任务（不出现在 ProjectsIndex 列表里）。
WORKFLOW_TYPE_POSTER_STYLE_GENERATE = "poster_style_library_generate"
# 该 workflow 的 step_key 和 worker action（用于 worker 端识别 + 写回结果）。
POSTER_STYLE_GENERATE_STEP_KEY = "poster_style_library_generate"
POSTER_STYLE_GENERATE_WORKER_ACTION = "poster_style_library_generate"

# storage_root 下的根 key。preset 二进制 / global index / per-user 旧 JSON 都挂在它下。
POSTER_STYLE_ROOT_KEY = POSTER_STYLE_LIBRARY_FOLDER

# GitHub Contents API 默认 URL（指向 cyeinfpro/Lumen 主仓 assets/poster-style-presets）。
# 可通过 env LUMEN_POSTER_STYLE_GITHUB_CONTENTS_URL 覆盖；与模特库 settings 同语义。
_DEFAULT_GITHUB_CONTENTS_URL = (
    "https://api.github.com/repos/cyeinfpro/Lumen/contents/"
    "assets/poster-style-presets?ref=main"
)

# system_settings spec key：与模特库的 model_library.sync_use_proxy_pool 类似命名。
POSTER_STYLE_SYNC_USE_PROXY_POOL_KEY = "poster_style.sync_use_proxy_pool"
POSTER_STYLE_SYNC_PROXY_NAME_KEY = "poster_style.sync_proxy_name"
# 同步权限的 system_setting：admin_only / any_authenticated / disabled
POSTER_STYLE_SYNC_MODE_KEY = "poster_style.sync_mode"
# 当 system_setting 缺省时的默认（与模特库默认一致：仅管理员能触发同步）
_DEFAULT_SYNC_MODE = "admin_only"

# 跨进程同步用可续租 lease；文件锁只覆盖短状态临界区。
POSTER_STYLE_SYNC_LEASE_SECONDS = 5 * 60
POSTER_STYLE_SYNC_LEASE_RENEW_SECONDS = 60

# GitHub Contents 遍历、响应体与整次同步预算。与 model library 同档，
# 足够覆盖风格库扩容，同时阻断异常目录树和超大响应。
POSTER_STYLE_MAX_GITHUB_DEPTH = 8
POSTER_STYLE_MAX_GITHUB_DIRECTORIES = 128
POSTER_STYLE_MAX_GITHUB_FILES = 4096
POSTER_STYLE_MAX_GITHUB_RESPONSE_BYTES = 4 * 1024 * 1024
POSTER_STYLE_MAX_GITHUB_METADATA_BYTES = 32 * 1024 * 1024
POSTER_STYLE_MAX_META_BYTES = 256 * 1024
POSTER_STYLE_MAX_SYNC_DOWNLOAD_BYTES = 512 * 1024 * 1024
POSTER_STYLE_MAX_INDEX_BYTES = 32 * 1024 * 1024
POSTER_STYLE_MAX_PRESET_ITEMS = 4096
POSTER_STYLE_MAX_REDIRECTS = 3

# 与 meta.json category 字段对齐的文件夹名映射。
# 与 README.md 的目录约定一致：00_user_favorites 留给用户收藏 placeholder。
POSTER_STYLE_FOLDER_BY_CATEGORY: MappingProxyType[str, str] = MappingProxyType(
    {
        "user_favorites": "00_user_favorites",
        "illustration": "01_flat_illustration",
        "3d": "02_3d_render",
        "minimal": "03_minimal_typography",
        "retro": "04_retro_pop",
        "traditional": "05_chinese_traditional",
        "photo": "06_editorial_photo",
        "other": "99_other",
    }
)

# 反向映射 + 数字前缀容忍：与模特库一样支持 "01_xxx" / "xxx" 双写。
_FOLDER_TO_CATEGORY: MappingProxyType[str, str] = MappingProxyType(
    {folder: cat for cat, folder in POSTER_STYLE_FOLDER_BY_CATEGORY.items()}
)

# 用户输入 prompt 经过的英文风格关键词归一化：跟 meta.json 的英文 prompt 风格保持一致。
# 后续 generation prompt 注入时优先用这里收敛过的标签。
_CATEGORY_ALIASES: MappingProxyType[str, str] = MappingProxyType(
    {
        # illustration
        "illustration": "illustration",
        "illustrated": "illustration",
        "illustrations": "illustration",
        "flat": "illustration",
        "flat_illustration": "illustration",
        "vector": "illustration",
        "vector_illustration": "illustration",
        "扁平": "illustration",
        "插画": "illustration",
        "矢量": "illustration",
        # 3d
        "3d": "3d",
        "3d_render": "3d",
        "3drender": "3d",
        "render": "3d",
        "三维": "3d",
        "立体": "3d",
        # minimal
        "minimal": "minimal",
        "minimal_typography": "minimal",
        "minimalism": "minimal",
        "minimalist": "minimal",
        "typography": "minimal",
        "极简": "minimal",
        "简约": "minimal",
        "字体": "minimal",
        # retro
        "retro": "retro",
        "pop": "retro",
        "retro_pop": "retro",
        "复古": "retro",
        "波普": "retro",
        # traditional
        "traditional": "traditional",
        "chinese_traditional": "traditional",
        "chinese": "traditional",
        "中式": "traditional",
        "国风": "traditional",
        "东方": "traditional",
        # photo
        "photo": "photo",
        "editorial_photo": "photo",
        "photography": "photo",
        "editorial": "photo",
        "摄影": "photo",
        "杂志": "photo",
        # other
        "other": "other",
    }
)

# 只保护同一进程内的短状态临界区；跨进程互斥由 flock + lease 完成。
# 任何网络请求或大文件 I/O 都不得放在这个锁内。
_SYNC_LOCK = asyncio.Lock()


@contextmanager
def _poster_style_sync_file_lock(path: Path) -> Iterator[None]:
    """Serialize short sync-state mutations across API worker processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- 元数据解析 helper -------------------------------------------------------

def _github_contents_url() -> str:
    """从 env 拿 GitHub Contents API URL。env 未设时回退到 cyeinfpro/Lumen 主仓。

    与模特库不同，风格库的 URL 不暴露在 Settings 上（V1.1 第一版尽量减少 config 表面），
    需要时改 env LUMEN_POSTER_STYLE_GITHUB_CONTENTS_URL 即可。
    """
    raw = os.environ.get("LUMEN_POSTER_STYLE_GITHUB_CONTENTS_URL", "").strip()
    return raw or _DEFAULT_GITHUB_CONTENTS_URL


def _normalize_category(value: Any) -> str:
    """把任意 string 归整到合法 PosterStyleCategory。无法识别返回 'other'。"""
    if not isinstance(value, str):
        return "user_favorites"
    raw = value.strip()
    if not raw:
        return "user_favorites"
    # 数字前缀容忍："01_flat_illustration" / "flat_illustration" / "illustration"
    cat_from_folder = _category_from_folder_name(raw)
    if cat_from_folder is not None:
        return cat_from_folder
    key = re.sub(r"[\s\-]+", "_", raw.lower()).strip("_")
    if not key:
        return "user_favorites"
    if key in POSTER_STYLE_CATEGORIES and key != "all":
        return key
    aliased = _CATEGORY_ALIASES.get(key)
    if aliased is not None:
        return aliased
    return "other"


def _category_from_folder_name(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    if raw in _FOLDER_TO_CATEGORY:
        return _FOLDER_TO_CATEGORY[raw]
    # 去掉 "NN_" 前缀再查：assets/poster-style-presets/01_flat_illustration → flat_illustration
    normalized = re.sub(r"^\d{1,3}[-_]", "", raw)
    if normalized in POSTER_STYLE_CATEGORIES and normalized != "all":
        return normalized
    alt = normalized.replace("-", "_")
    if alt in POSTER_STYLE_CATEGORIES and alt != "all":
        return alt
    return None


def _poster_style_folder_for_category(category: Any) -> str:
    cat = _normalize_category(category)
    return POSTER_STYLE_FOLDER_BY_CATEGORY.get(
        cat, POSTER_STYLE_FOLDER_BY_CATEGORY["other"]
    )


def _library_item_url(item_id: str, kind: str) -> str:
    """preset / 没有 image_id 的库条目走 API 端点拿二进制（apparel 同语义）。"""
    return f"/api/poster-styles/items/{item_id}/{kind}"


def _library_sample_url(item_id: str, sample_index: int) -> str:
    """preset 多张 sample 用：sample_index=0 通常 = cover。"""
    return f"/api/poster-styles/items/{item_id}/samples/{int(sample_index)}"


def _preset_id_from_meta(meta: dict[str, Any], directory: Path | None = None) -> str:
    """优先用 meta.json 里显式的 preset_id；没有就回退到目录名（去掉 NN_ 前缀）。"""
    raw = str(meta.get("preset_id") or "").strip()
    if raw:
        return raw[:120]
    if directory is not None:
        dirname = directory.name
        return re.sub(r"^\d{1,3}[-_]", "", dirname).strip() or dirname
    return ""


def _title_from_preset_id(preset_id: str) -> str:
    words = [part for part in re.split(r"[-_]+", preset_id) if part]
    return " ".join(w.capitalize() for w in words) or preset_id


def _load_preset_meta(directory: Path) -> dict[str, Any] | None:
    """读 ``<directory>/meta.json``，返回 dict 或 None。

    优先 graceful：文件不存在 / JSON 无效 / 非 dict → None，不 raise（让调用方按
    "preset_id 缺失"跳过本目录）。
    """
    if not isinstance(directory, Path):
        return None
    try:
        meta_path = directory / "meta.json"
    except Exception:  # noqa: BLE001 — 路径拼接极端边界
        return None
    if not meta_path.is_file():
        return None
    try:
        with meta_path.open("rb") as handle:
            raw = handle.read(POSTER_STYLE_MAX_META_BYTES + 1)
    except OSError as exc:
        logger.info("poster style meta.json read failed dir=%s err=%s", directory, exc)
        return None
    if len(raw) > POSTER_STYLE_MAX_META_BYTES:
        logger.info(
            "poster style meta.json exceeds byte limit dir=%s limit=%d",
            directory,
            POSTER_STYLE_MAX_META_BYTES,
        )
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        logger.info("poster style meta.json parse failed dir=%s err=%s", directory, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _clean_str_list(
    values: Any, *, max_items: int, max_len: int
) -> list[str]:
    """meta.json 里 palette / tags / recommended_aspects 都是 string 数组的清洗。"""
    if not isinstance(values, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, (str, int, float)):
            continue
        item = str(raw).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item[:max_len])
        if len(out) >= max_items:
            break
    return out


def _normalize_palette(values: Any) -> list[str]:
    """palette 限制为 ≤ 12 项，每项 ≤ 16 字符（#RRGGBB / rgba(255,...)）。"""
    cleaned = _clean_str_list(values, max_items=12, max_len=16)
    return cleaned


def _normalize_recommended_aspects(values: Any) -> list[str]:
    """meta.json 没填或填空时回退到 ``POSTER_STYLE_DEFAULT_ASPECTS``。"""
    cleaned = _clean_str_list(values, max_items=8, max_len=12)
    return cleaned or list(POSTER_STYLE_DEFAULT_ASPECTS)


def _normalize_style_tags(values: Any) -> list[str]:
    """与模特库 _clean_style_tags 同语义：去重、strip、≤ 32 字、≤ 12 项。"""
    return _clean_str_list(values, max_items=12, max_len=32)


def _metadata_from_meta_json(
    meta: dict[str, Any],
    *,
    directory: Path | None = None,
    category_hint: str | None = None,
) -> dict[str, Any] | None:
    """从 meta.json dict 抽出一条 preset 的标准化元数据。

    Returns None 当 preset_id 抓不出来时。
    """
    preset_id = _preset_id_from_meta(meta, directory)
    if not preset_id:
        return None
    raw_category = meta.get("category") or category_hint
    category = _normalize_category(raw_category)
    title = str(meta.get("title") or _title_from_preset_id(preset_id)).strip()[:120]
    return {
        "preset_id": preset_id,
        "version": int(meta.get("version") or 1),
        "title": title,
        "category": category,
        "library_folder": _poster_style_folder_for_category(category),
        "mood": _clean_optional_text(meta.get("mood"), max_len=120),
        "prompt_template": _clean_optional_text(
            meta.get("prompt_template"), max_len=2000
        ),
        "palette": _normalize_palette(meta.get("palette")),
        "recommended_aspects": _normalize_recommended_aspects(
            meta.get("recommended_aspects")
        ),
        "style_tags": _normalize_style_tags(meta.get("tags") or meta.get("style_tags")),
    }


def _clean_optional_text(value: Any, *, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]


# --- 本地扫描（assets/poster-style-presets/） -----------------------------

def _scan_local_presets(root: Path) -> list[dict[str, Any]]:
    """扫描仓库内的 ``assets/poster-style-presets/`` 目录，返回标准化元数据列表。

    用于首次启动或本地开发时 bootstrap（不打 GitHub），逻辑与
    ``_sync_library_presets_from_github_folder`` 一致：每个子目录读 meta.json，
    枚举同目录下的 sample 图（按名字字典序，跳过 *.thumb.* 视为缩略图）。

    Returns:
        每项形如：
        ``{"preset_id", "version", "title", "category", "library_folder",
          "mood", "prompt_template", "palette", "recommended_aspects",
          "style_tags", "samples": [{"name", "path", "is_thumb"}, ...]}``
    """
    if not isinstance(root, Path) or not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        # README / 元数据文件夹之外的目录才扫
        meta = _load_preset_meta(sub)
        if meta is None:
            continue
        category_hint = _category_from_folder_name(sub.name)
        parsed = _metadata_from_meta_json(
            meta, directory=sub, category_hint=category_hint
        )
        if parsed is None:
            continue
        # 列样图（不含 thumb，限制总数 ≤ POSTER_STYLE_MAX_SAMPLES）
        samples: list[dict[str, Any]] = []
        for entry in sorted(sub.iterdir(), key=lambda p: p.name.lower()):
            if not entry.is_file():
                continue
            suffix = entry.suffix.lower()
            if suffix not in POSTER_STYLE_IMAGE_SUFFIXES:
                continue
            stem = entry.stem
            is_thumb = stem.endswith(".thumb")
            base_stem = stem[: -len(".thumb")] if is_thumb else stem
            samples.append(
                {
                    "name": entry.name,
                    "path": str(entry.relative_to(root.parent)) if entry.is_absolute() else str(entry),
                    "is_thumb": is_thumb,
                    "base_stem": base_stem,
                    "suffix": suffix,
                }
            )
            if len(samples) >= POSTER_STYLE_MAX_SAMPLES * 2:
                # 给 thumb 留一倍空间，外层 dedupe
                break
        parsed["samples"] = samples
        parsed["directory_name"] = sub.name
        out.append(parsed)
    return out


# --- ID / Storage key helpers ----------------------------------------------

def _preset_item_id(preset_id: str, version: int) -> str:
    """与模特库一致的"preset:<id>:v<n>"约定，前端按 item.id 前缀区分 preset/user。"""
    return f"preset:{preset_id}:v{int(version)}"


def _preset_storage_key(preset_id: str, version: int, sample_name: str) -> str:
    """落盘 key：<root>/presets/<preset_id>/v<n>/<sample_name>。

    与模特库（每条 1 张）相比，这里 sample_name 区分多张 sample。
    """
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", sample_name).strip("._-") or "sample"
    return f"{POSTER_STYLE_ROOT_KEY}/presets/{preset_id}/v{int(version)}/{safe_name}"


def _preset_thumb_storage_key(
    preset_id: str, version: int, base_sample_name: str, suffix: str
) -> str:
    """thumb 落盘 key：与原图同目录，stem 加 .thumb 后缀（与 GitHub 端约定一致）。"""
    safe_base = re.sub(r"[^a-zA-Z0-9._-]+", "_", base_sample_name).strip("._-") or "sample"
    safe_suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ".webp"
    return f"{POSTER_STYLE_ROOT_KEY}/presets/{preset_id}/v{int(version)}/{safe_base}.thumb{safe_suffix}"


__all__ = [
    "POSTER_STYLE_CATEGORIES",
    "POSTER_STYLE_DEFAULT_ASPECTS",
    "POSTER_STYLE_FETCH_TIMEOUT_S",
    "POSTER_STYLE_FOLDER_BY_CATEGORY",
    "POSTER_STYLE_GENERATE_STEP_KEY",
    "POSTER_STYLE_GENERATE_WORKER_ACTION",
    "POSTER_STYLE_IMAGE_SUFFIXES",
    "POSTER_STYLE_LIBRARY_FOLDER",
    "POSTER_STYLE_MAX_BINARY_BYTES",
    "POSTER_STYLE_MAX_GITHUB_DEPTH",
    "POSTER_STYLE_MAX_GITHUB_DIRECTORIES",
    "POSTER_STYLE_MAX_GITHUB_FILES",
    "POSTER_STYLE_MAX_GITHUB_METADATA_BYTES",
    "POSTER_STYLE_MAX_GITHUB_RESPONSE_BYTES",
    "POSTER_STYLE_MAX_INDEX_BYTES",
    "POSTER_STYLE_MAX_META_BYTES",
    "POSTER_STYLE_MAX_PRESET_ITEMS",
    "POSTER_STYLE_MAX_REDIRECTS",
    "POSTER_STYLE_MAX_SAMPLES",
    "POSTER_STYLE_MAX_SYNC_DOWNLOAD_BYTES",
    "POSTER_STYLE_PRESET_ROOT",
    "POSTER_STYLE_ROOT_KEY",
    "POSTER_STYLE_SCHEMA_VERSION",
    "POSTER_STYLE_SOURCES",
    "POSTER_STYLE_SYNC_COOLDOWN_S",
    "POSTER_STYLE_SYNC_FAILURE_COOLDOWN_S",
    "POSTER_STYLE_SYNC_LEASE_RENEW_SECONDS",
    "POSTER_STYLE_SYNC_LEASE_SECONDS",
    "POSTER_STYLE_SYNC_MODES",
    "POSTER_STYLE_SYNC_MODE_KEY",
    "POSTER_STYLE_SYNC_PROXY_NAME_KEY",
    "POSTER_STYLE_SYNC_USE_PROXY_POOL_KEY",
    "WORKFLOW_TYPE_POSTER_STYLE_GENERATE",
    "_DEFAULT_GITHUB_CONTENTS_URL",
    "_DEFAULT_SYNC_MODE",
    "_SYNC_LOCK",
    "_category_from_folder_name",
    "_clean_optional_text",
    "_clean_str_list",
    "_github_contents_url",
    "_library_item_url",
    "_library_sample_url",
    "_load_preset_meta",
    "_metadata_from_meta_json",
    "_normalize_category",
    "_normalize_palette",
    "_normalize_recommended_aspects",
    "_normalize_style_tags",
    "_poster_style_folder_for_category",
    "_poster_style_sync_file_lock",
    "_preset_id_from_meta",
    "_preset_item_id",
    "_preset_storage_key",
    "_preset_thumb_storage_key",
    "_scan_local_presets",
    "_title_from_preset_id",
]
