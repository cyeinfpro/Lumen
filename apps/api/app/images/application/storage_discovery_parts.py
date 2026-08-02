"""存储孤儿发现的路径/键谓词(目录结构与安全校验)。

从 images/application/storage_maintenance.py 拆出,保持主文件在 general
module 行数上限内。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


def is_safe_storage_segment(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and "\\" not in value


def is_attempt_segment(value: str) -> bool:
    return value.isascii() and value.isdigit()


def is_completion_attempt_segment(value: str) -> bool:
    if is_attempt_segment(value):
        return True
    prefix = "execution-"
    marker = "-attempt-"
    if not value.startswith(prefix) or marker not in value:
        return False
    execution, attempt = value[len(prefix) :].split(marker, 1)
    return is_attempt_segment(execution) and is_attempt_segment(attempt)


def is_storage_leaf_directory_parts(parts: tuple[str, ...]) -> bool:
    if len(parts) < 3 or parts[0] != "u":
        return False
    if parts[2] == "uploads":
        return len(parts) == 3
    if parts[2] == "g":
        if len(parts) == 4:
            return True
        if len(parts) == 6 and parts[4] == "attempts":
            return is_attempt_segment(parts[5])
        return (
            len(parts) == 8
            and parts[4] == "executions"
            and is_attempt_segment(parts[5])
            and parts[6] == "attempts"
            and is_attempt_segment(parts[7])
        )
    if parts[2] == "completion-tools":
        if (
            len(parts) == 7
            and parts[4] == "attempts"
            and is_completion_attempt_segment(parts[5])
        ):
            return True
        return (
            len(parts) == 9
            and parts[4] == "executions"
            and is_attempt_segment(parts[5])
            and parts[6] == "attempts"
            and is_attempt_segment(parts[7])
        )
    if parts[2] == "v":
        return len(parts) == 4 or (len(parts) == 6 and parts[4] == "final")
    if parts[2] == "vref":
        return len(parts) == 4
    return parts[2] == "storyboards" and len(parts) == 6 and parts[4] == "assembly"


def is_storage_leaf_directory_key(key: str) -> bool:
    if not key or key.startswith("/") or "\x00" in key or "\\" in key:
        return False
    parts = tuple(key.split("/"))
    return all(is_safe_storage_segment(part) for part in parts) and (
        is_storage_leaf_directory_parts(parts)
    )


def is_image_file_storage_key(key: str) -> bool:
    if not key or key.startswith("/") or "\x00" in key or "\\" in key:
        return False
    parts = tuple(key.split("/"))
    if not all(is_safe_storage_segment(part) for part in parts):
        return False
    if len(parts) < 4 or not is_storage_leaf_directory_parts(parts[:-1]):
        return False
    name = parts[-1]
    if name.startswith(".") or name.endswith(".tmp"):
        # Leaf directories also host housekeeping files that are never
        # registered artifacts: the flock publish mutex
        # (".artifact-publish.lock"), atomic-write temp files
        # (".{name}.{token}.tmp"), and variant staging temps
        # (".lumen-variant-*.webp"). Treating them as orphan candidates
        # could unlink a held lock (breaking the publish mutex) or an
        # in-progress write.
        return False
    return True


def is_safe_directory_path(root: Path, directory: Path) -> bool:
    try:
        relative = directory.relative_to(root)
    except ValueError:
        return False
    if not all(is_safe_storage_segment(part) for part in relative.parts):
        return False

    current = root
    try:
        root_info = os.stat(root, follow_symlinks=False)
    except OSError:
        return False
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return False

    for part in relative.parts:
        current /= part
        try:
            info = os.stat(current, follow_symlinks=False)
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return False
    return True
