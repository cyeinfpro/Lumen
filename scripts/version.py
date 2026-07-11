#!/usr/bin/env python3
"""Manage Lumen's product version from a single VERSION file.

The VERSION file stores the product version without a leading "v" (for example
1.2.3). Git tags and Docker tags add the "v" prefix where appropriate.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - compatibility with older python3
    tomllib = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "VERSION"

PYPROJECT_FILES = [
    ROOT / "pyproject.toml",
    ROOT / "apps/api/pyproject.toml",
    ROOT / "apps/worker/pyproject.toml",
    ROOT / "apps/tgbot/pyproject.toml",
    ROOT / "packages/core/pyproject.toml",
]
WEB_PACKAGE_JSON = ROOT / "apps/web/package.json"
WEB_PACKAGE_LOCK = ROOT / "apps/web/package-lock.json"
UV_LOCK = ROOT / "uv.lock"
CORE_INIT = ROOT / "packages/core/lumen_core/__init__.py"
UV_LOCK_PACKAGE_NAMES = {
    "lumen",
    "lumen-api",
    "lumen-worker",
    "lumen-tgbot",
    "lumen-core",
}
CURRENT_RELEASE_JSON_CANDIDATES = (
    ROOT / ".lumen_release.json",
    ROOT / "current" / ".lumen_release.json",
)

SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$"
)


@dataclass(frozen=True)
class VersionTarget:
    path: Path
    label: str
    pattern: re.Pattern[str]
    replacement: str


def read_product_version() -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not SEMVER_RE.fullmatch(version):
        raise SystemExit(
            f"VERSION must be semver without a leading 'v' (got {version!r})"
        )
    return version


def targets(version: str) -> list[VersionTarget]:
    pyproject_pattern = re.compile(r'(?m)^(version\s*=\s*)"([^"]+)"')
    items = [
        VersionTarget(
            path=path,
            label=str(path.relative_to(ROOT)),
            pattern=pyproject_pattern,
            replacement=rf'\g<1>"{version}"',
        )
        for path in PYPROJECT_FILES
    ]
    items.append(
        VersionTarget(
            path=CORE_INIT,
            label=str(CORE_INIT.relative_to(ROOT)),
            pattern=re.compile(r'(?m)^(__version__\s*=\s*)"([^"]+)"'),
            replacement=rf'\g<1>"{version}"',
        )
    )
    return items


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_uv_lock_packages(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        data = tomllib.loads(text)
        packages = data.get("package", [])
        return [item for item in packages if isinstance(item, dict)]

    packages: list[dict[str, Any]] = []
    for block in re.split(r"(?m)^\[\[package\]\]\s*$", text):
        if not block.strip():
            continue
        name_match = re.search(r'(?m)^name\s*=\s*"([^"]+)"', block)
        version_match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', block)
        if name_match:
            packages.append(
                {
                    "name": name_match.group(1),
                    "version": version_match.group(1) if version_match else None,
                }
            )
    return packages


def current_release_json_path() -> Path | None:
    for path in CURRENT_RELEASE_JSON_CANDIDATES:
        if path.exists():
            return path
    return None


def rolling_tag_allowed() -> bool:
    return os.environ.get("LUMEN_ALLOW_ROLLING_TAG") == "1"


def allowed_runtime_image_tags(version: str) -> set[str]:
    allowed = {f"v{version}"}
    if rolling_tag_allowed():
        allowed.add("main")
    return allowed


def current_value(target: VersionTarget) -> str | None:
    text = target.path.read_text(encoding="utf-8")
    match = target.pattern.search(text)
    if not match:
        return None
    return match.group(2)


def sync_target(target: VersionTarget) -> bool:
    text = target.path.read_text(encoding="utf-8")
    new_text, count = target.pattern.subn(target.replacement, text, count=1)
    if count != 1:
        raise SystemExit(f"{target.label}: version field not found")
    if new_text == text:
        return False
    target.path.write_text(new_text, encoding="utf-8")
    return True


def check() -> int:
    version = read_product_version()
    mismatches: list[str] = []
    for target in targets(version):
        value = current_value(target)
        if value is None:
            mismatches.append(f"{target.label}: missing version field")
        elif value != version:
            mismatches.append(f"{target.label}: {value} != {version}")

    package_json = read_json(WEB_PACKAGE_JSON)
    if package_json.get("version") != version:
        mismatches.append(
            f"{WEB_PACKAGE_JSON.relative_to(ROOT)}: {package_json.get('version')} != {version}"
        )

    package_lock = read_json(WEB_PACKAGE_LOCK)
    if package_lock.get("version") != version:
        mismatches.append(
            f"{WEB_PACKAGE_LOCK.relative_to(ROOT)}: {package_lock.get('version')} != {version}"
        )
    root_package = package_lock.get("packages", {}).get("", {})
    if root_package.get("version") != version:
        mismatches.append(
            f"{WEB_PACKAGE_LOCK.relative_to(ROOT)} packages['']: "
            f"{root_package.get('version')} != {version}"
        )

    if UV_LOCK.exists():
        try:
            lock_packages = read_uv_lock_packages(UV_LOCK)
        except Exception as exc:
            mismatches.append(f"{UV_LOCK.relative_to(ROOT)}: cannot parse ({exc})")
        else:
            seen_lock_packages: set[str] = set()
            for item in lock_packages:
                name = item.get("name")
                if name not in UV_LOCK_PACKAGE_NAMES:
                    continue
                seen_lock_packages.add(str(name))
                if item.get("version") != version:
                    mismatches.append(
                        f"{UV_LOCK.relative_to(ROOT)} package {name}: "
                        f"{item.get('version')} != {version}"
                    )
            missing = UV_LOCK_PACKAGE_NAMES - seen_lock_packages
            for name in sorted(missing):
                mismatches.append(f"{UV_LOCK.relative_to(ROOT)} package {name}: missing")

    current_release_json = current_release_json_path()
    if current_release_json is not None:
        try:
            release = read_json(current_release_json)
        except json.JSONDecodeError as exc:
            mismatches.append(f"{current_release_json.relative_to(ROOT)}: invalid JSON ({exc})")
        else:
            if not isinstance(release, dict):
                mismatches.append(f"{current_release_json.relative_to(ROOT)}: JSON root is not object")
            else:
                image_tag = release.get("image_tag")
                if image_tag == "main" and not rolling_tag_allowed():
                    mismatches.append(
                        f"{current_release_json.relative_to(ROOT)} image_tag: "
                        "'main' requires LUMEN_ALLOW_ROLLING_TAG=1"
                    )
                elif image_tag not in allowed_runtime_image_tags(version):
                    mismatches.append(
                        f"{current_release_json.relative_to(ROOT)} image_tag: "
                        f"{image_tag!r} != 'v{version}'"
                    )
                for key in ("id", "sha"):
                    value = release.get(key)
                    if value is not None and not isinstance(value, str):
                        mismatches.append(
                            f"{current_release_json.relative_to(ROOT)} {key}: not a string"
                        )

    if mismatches:
        print("Version mismatch:", file=sys.stderr)
        for item in mismatches:
            print(f"  - {item}", file=sys.stderr)
        print("Run: python3 scripts/version.py sync", file=sys.stderr)
        if any(item.startswith(str(UV_LOCK.relative_to(ROOT))) for item in mismatches):
            print("Run: uv lock", file=sys.stderr)
        return 1

    print(f"version ok: {version}")
    return 0


def sync() -> int:
    version = read_product_version()
    changed = []
    for target in targets(version):
        if sync_target(target):
            changed.append(target.label)

    package_json = read_json(WEB_PACKAGE_JSON)
    if package_json.get("version") != version:
        package_json["version"] = version
        write_json(WEB_PACKAGE_JSON, package_json)
        changed.append(str(WEB_PACKAGE_JSON.relative_to(ROOT)))

    package_lock = read_json(WEB_PACKAGE_LOCK)
    lock_changed = False
    if package_lock.get("version") != version:
        package_lock["version"] = version
        lock_changed = True
    root_package = package_lock.get("packages", {}).get("", {})
    if root_package.get("version") != version:
        root_package["version"] = version
        lock_changed = True
    if lock_changed:
        write_json(WEB_PACKAGE_LOCK, package_lock)
        changed.append(str(WEB_PACKAGE_LOCK.relative_to(ROOT)))
    if changed:
        print(f"synced version {version}:")
        for label in changed:
            print(f"  - {label}")
    else:
        print(f"all version targets already at {version}")
    return 0


def docker_tags() -> int:
    version = read_product_version()
    tags = [f"v{version}"]
    if "-" not in version:
        major, minor, _patch = version.split(".", 2)
        tags.extend([f"v{major}.{minor}", f"v{major}", "latest"])
    print("\n".join(tags))
    return 0


def assert_tag(tag: str) -> int:
    version = read_product_version()
    expected = f"v{version}"
    if tag != expected:
        print(f"tag mismatch: got {tag!r}, expected {expected!r}", file=sys.stderr)
        return 1
    print(f"tag ok: {tag}")
    return 0


def print_runtime() -> int:
    version = read_product_version()
    release: dict[str, object] = {}
    current_release_json = current_release_json_path()
    if current_release_json is not None:
        release = read_json(current_release_json)
    image_tag = release.get("image_tag") or f"v{version}"
    if not isinstance(image_tag, str):
        image_tag = f"v{version}"
    release_id = release.get("id")
    sha = release.get("sha")
    payload = {
        "version": version,
        "image_tag": image_tag,
        "release_id": release_id,
        "sha": sha,
    }
    mismatches: list[str] = []
    if image_tag not in allowed_runtime_image_tags(version):
        mismatches.append(f"image_tag={image_tag} != v{version}")
    if release_id is not None and not isinstance(release_id, str):
        mismatches.append("release_id not string")
    if sha is not None and not isinstance(sha, str):
        mismatches.append("sha not string")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if mismatches:
        print("runtime mismatch:", file=sys.stderr)
        for item in mismatches:
            print(f"  - {item}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("print", help="print product version")
    sub.add_parser("check", help="verify all version targets match VERSION")
    sub.add_parser("sync", help="rewrite version targets to match VERSION")
    sub.add_parser("docker-tags", help="print release Docker tags for VERSION")
    sub.add_parser("print-runtime", help="print runtime version / image tag / release metadata")
    tag_parser = sub.add_parser("assert-tag", help="verify a git tag matches VERSION")
    tag_parser.add_argument("tag", help="tag name, e.g. v1.2.3")

    args = parser.parse_args(argv)
    if args.cmd == "print":
        print(read_product_version())
        return 0
    if args.cmd == "check":
        return check()
    if args.cmd == "sync":
        return sync()
    if args.cmd == "docker-tags":
        return docker_tags()
    if args.cmd == "assert-tag":
        return assert_tag(args.tag)
    if args.cmd == "print-runtime":
        return print_runtime()
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
