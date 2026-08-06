from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update" / "release" / "image_proof_store.py"
SPEC = importlib.util.spec_from_file_location("image_proof_store", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
IMAGE_PROOF_STORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMAGE_PROOF_STORE)


def _create_source_tree(root: Path) -> Path:
    source = root / "scripts" / "update" / "release" / "worker.sh"
    source.parent.mkdir(parents=True)
    source.write_text("#!/bin/sh\nprintf 'ready\\n'\n", encoding="utf-8")
    source.chmod(0o755)
    return source


def test_source_tree_hash_ignores_runtime_artifacts(tmp_path: Path) -> None:
    _create_source_tree(tmp_path)
    baseline = IMAGE_PROOF_STORE.source_tree_sha256(tmp_path)

    (tmp_path / "scripts.lumen-self-update.lock").write_text(
        "runtime lock\n",
        encoding="utf-8",
    )
    pycache = tmp_path / "scripts" / "update" / "release" / "__pycache__"
    pycache.mkdir()
    (pycache / "image_proof_store.cpython-312.pyc").write_bytes(b"cached bytecode")
    (tmp_path / "scripts" / "update" / "runtime.pyc").write_bytes(b"more bytecode")
    for suffix in ("source", "integrity", "last"):
        (tmp_path / "scripts" / f".lumen-self-update.{suffix}").write_text(
            f"{suffix}\n",
            encoding="utf-8",
        )

    assert IMAGE_PROOF_STORE.source_tree_sha256(tmp_path) == baseline


def test_source_tree_hash_detects_source_content_and_mode_changes(
    tmp_path: Path,
) -> None:
    source = _create_source_tree(tmp_path)
    baseline = IMAGE_PROOF_STORE.source_tree_sha256(tmp_path)

    source.write_text("#!/bin/sh\nprintf 'changed\\n'\n", encoding="utf-8")
    assert IMAGE_PROOF_STORE.source_tree_sha256(tmp_path) != baseline

    source.write_text("#!/bin/sh\nprintf 'ready\\n'\n", encoding="utf-8")
    assert IMAGE_PROOF_STORE.source_tree_sha256(tmp_path) == baseline

    source.chmod(0o644)
    assert IMAGE_PROOF_STORE.source_tree_sha256(tmp_path) != baseline
