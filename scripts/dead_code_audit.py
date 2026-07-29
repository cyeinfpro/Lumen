#!/usr/bin/env python3
"""Verify that reviewed high-confidence dead code stays retired."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RETIRED_PATHS = (
    "apps/api/app/workflows/adapters/model_library_tagging.py",
    "apps/api/app/workflows/domain/planning.py",
    "apps/worker/app/upstream_parts/errors.py",
    "apps/web/src/app/(chat)/_hooks/useCompactConversation.ts",
    "image-job/image_job/api/__init__.py",
    "image-job/image_job/ports/artifacts.py",
    "packages/core/lumen_core/alembic_expand.py",
)
RETIRED_IMPORT_MARKERS = (
    "workflows.adapters.model_library_tagging",
    "workflows.domain.planning",
    "upstream_parts.errors",
    "(chat)/_hooks/useCompactConversation",
    "lumen_core.alembic_expand",
)
RETIRED_NPM_PACKAGES = ("gsap", "@gsap/react")
SOURCE_SUFFIXES = {".js", ".mjs", ".py", ".ts", ".tsx"}


def audit(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    for relative in RETIRED_PATHS:
        if (root / relative).exists():
            errors.append(f"retired path still exists: {relative}")

    source_roots = ("apps", "image-job", "packages")
    for source_root in source_roots:
        base = root / source_root
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if (
                not path.is_file()
                or path.suffix not in SOURCE_SUFFIXES
                or "tests" in path.parts
                or "__pycache__" in path.parts
            ):
                continue
            source = path.read_text(encoding="utf-8", errors="ignore")
            for marker in RETIRED_IMPORT_MARKERS:
                if marker in source:
                    errors.append(
                        f"retired import marker remains: "
                        f"{path.relative_to(root)}:{marker}"
                    )

    package_json = root / "apps/web/package.json"
    if package_json.is_file():
        payload = json.loads(package_json.read_text(encoding="utf-8"))
        declared = {
            name
            for section in ("dependencies", "devDependencies", "peerDependencies")
            for name in (payload.get(section) or {})
        }
        for package in RETIRED_NPM_PACKAGES:
            if package in declared:
                errors.append(f"retired npm dependency remains: {package}")

    lock_path = root / "apps/web/package-lock.json"
    if lock_path.is_file():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        packages = lock.get("packages") or {}
        for package in RETIRED_NPM_PACKAGES:
            if f"node_modules/{package}" in packages:
                errors.append(f"retired npm lock entry remains: {package}")
    return errors


def main() -> int:
    errors = audit()
    if errors:
        print("Dead-code retirement audit failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(
        "Dead-code retirement audit passed: "
        f"{len(RETIRED_PATHS)} paths and {len(RETIRED_NPM_PACKAGES)} dependencies."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
