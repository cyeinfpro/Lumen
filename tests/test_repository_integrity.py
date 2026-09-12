from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

SPEC = spec_from_file_location(
    "lumen_repository_integrity", Path(__file__).resolve().parents[1] / "scripts/repository_integrity.py",
)
assert SPEC is not None and SPEC.loader is not None
integrity = module_from_spec(SPEC)
SPEC.loader.exec_module(integrity)


@pytest.mark.parametrize("path", [
    "../outside.py", "/outside.py", ".env", "apps/api/.env.local", "id_ed25519",
    "credentials/account.json", "node_modules/package/file.js", "var/storage/data.json",
    ".audit_state/local.json", "tmp/scratch.py", "memory/logs/log.txt",
])
def test_excludes_credentials_and_non_source_paths_before_reading(path: str) -> None:
    assert integrity.exclusion_reason(path) is not None


@pytest.mark.parametrize("path", [
    "apps/api/app/services/memory/runner.py", "apps/web/src/lib/memory/store.ts",
    ".env.example", "scripts/update/apply.py", "docs/audits/report.md",
])
def test_does_not_exclude_real_source_modules(path: str) -> None:
    assert integrity.exclusion_reason(path) is None


def test_compiles_without_executing_source(tmp_path: Path) -> None:
    path = tmp_path / "source.py"
    path.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
    entry, _ = integrity.inspect_file(tmp_path, "source.py")
    assert entry["status"] == "checked"
    assert "python_compile_without_execution" in entry["checks"]
    assert len(entry["sha256"]) == 64


def test_syntax_errors_fail_with_no_source_in_report(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("def broken(\n", encoding="utf-8")
    entry, _ = integrity.inspect_file(tmp_path, "source.py")
    assert entry["status"] == "failed"
    assert entry["error_type"] == "SyntaxError"
    assert "def broken" not in str(entry)


def test_excludes_symlinked_parent_without_reading_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "data.py").write_text("broken(\n", encoding="utf-8")
    (tmp_path / "linked").symlink_to(target, target_is_directory=True)
    entry, _ = integrity.inspect_file(tmp_path, "linked/data.py")
    assert entry["status"] == "excluded"
    assert entry["reason"] == "symlink"
    assert "sha256" not in entry


def test_fixture_syntax_is_recorded_but_not_enforced(tmp_path: Path) -> None:
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures/broken.json").write_text("{broken", encoding="utf-8")
    entry, _ = integrity.inspect_file(tmp_path, "fixtures/broken.json")
    assert entry["status"] == "checked"
    assert "fixture_syntax_not_enforced" in entry["checks"]


def test_jsonc_uses_the_typescript_gate(tmp_path: Path) -> None:
    (tmp_path / "tsconfig.json").write_text('{// comment\n"include": [],}', encoding="utf-8")
    entry, _ = integrity.inspect_file(tmp_path, "tsconfig.json")
    assert entry["status"] == "checked"
    assert "jsonc_requires_separate_typescript_gate" in entry["checks"]


def test_duplicate_keys_are_candidates_not_automatic_fixes() -> None:
    candidates = integrity.python_candidates("config = {'a': 1, 'a': 2}\n", "app.py")
    assert len(candidates) == 1
    assert candidates[0]["kind"] == "duplicate_literal_dict_key"
    assert integrity.python_candidates("config = {'a': 1, 'a': 2}\n", "tests/test_data.py") == []


def test_report_counts_each_path_and_does_not_hash_itself(tmp_path: Path) -> None:
    (tmp_path / "source.py").write_text("answer = 42\n", encoding="utf-8")
    report_path = tmp_path / "report.json"
    report = integrity.build_report(tmp_path, ["source.py", ".env", "missing.py", "report.json"], report_path)
    assert report["counts"] == {"checked": 1, "excluded": 1, "absent": 1}
    assert len(report["files"]) == 3
    assert report["limitations"]
