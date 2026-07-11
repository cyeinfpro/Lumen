from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_guard() -> ModuleType:
    path = ROOT / "scripts" / "release_manifest_guard.py"
    spec = importlib.util.spec_from_file_location("release_manifest_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest(tag: str = "v1.2.3") -> dict[str, object]:
    images = {}
    for index, service in enumerate(("api", "worker", "web", "tgbot"), start=1):
        digest = f"sha256:{index:064x}"
        repository = f"ghcr.io/cyeinfpro/lumen-{service}"
        images[service] = {
            "tag": f"{repository}:{tag}",
            "digest": digest,
            "immutable_ref": f"{repository}@{digest}",
        }
    return {
        "schema_version": 1,
        "version": tag,
        "commit_sha": "a" * 40,
        "short_sha": "a" * 7,
        "generated_at": "2026-07-11T00:00:00Z",
        "alembic_heads": ["0042_generation_billing_retry"],
        "images": images,
    }


def test_release_manifest_guard_validates_exact_image_set(tmp_path: Path) -> None:
    guard = _load_guard()
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(_manifest()), encoding="utf-8")

    loaded = guard.load_manifest(path, tag="v1.2.3")

    assert loaded["version"] == "v1.2.3"
    assert set(loaded["images"]) == {"api", "worker", "web", "tgbot"}


def test_release_manifest_guard_rejects_mutated_digest_or_tag(
    tmp_path: Path,
) -> None:
    guard = _load_guard()
    payload = _manifest()
    payload["images"]["api"]["immutable_ref"] = (  # type: ignore[index]
        "ghcr.io/cyeinfpro/lumen-api@sha256:" + ("f" * 64)
    )
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(guard.ManifestError, match="immutable ref mismatch"):
        guard.load_manifest(path, tag="v1.2.3")

    path.write_text(json.dumps(_manifest()), encoding="utf-8")
    with pytest.raises(guard.ManifestError, match="version mismatch"):
        guard.load_manifest(path, tag="v1.2.4")


def test_release_manifest_guard_resolves_alias_to_latest_stable_release() -> None:
    guard = _load_guard()
    payload = [
        {"tag_name": "v1.2.3", "draft": False, "prerelease": False},
        {"tag_name": "v1.2.10", "draft": False, "prerelease": False},
        {"tag_name": "v1.3.0", "draft": False, "prerelease": False},
        {"tag_name": "v1.2.11-rc.1", "draft": False, "prerelease": True},
        {"tag_name": "v2.0.0", "draft": True, "prerelease": False},
    ]

    assert guard.select_matching_release_tag(payload, alias="v1.2") == "v1.2.10"
    assert guard.select_matching_release_tag(payload, alias="v1") == "v1.3.0"

    with pytest.raises(guard.ManifestError, match="no concrete release"):
        guard.select_matching_release_tag(payload, alias="v3")
