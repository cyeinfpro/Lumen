#!/usr/bin/env python3
"""Manage Lumen's product version from a single VERSION file.

The VERSION file stores the product version without a leading "v" (for example
1.2.3). Git tags and Docker tags add the "v" prefix where appropriate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path


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
CORE_INIT = ROOT / "packages/core/lumen_core/__init__.py"

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

    if mismatches:
        print("Version mismatch:", file=sys.stderr)
        for item in mismatches:
            print(f"  - {item}", file=sys.stderr)
        print("Run: python3 scripts/version.py sync", file=sys.stderr)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("print", help="print product version")
    sub.add_parser("check", help="verify all version targets match VERSION")
    sub.add_parser("sync", help="rewrite version targets to match VERSION")
    sub.add_parser("docker-tags", help="print release Docker tags for VERSION")
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
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
