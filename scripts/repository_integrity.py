#!/usr/bin/env python3
"""Inventory every first-party file and run non-executing syntax checks.

This is NOT proof of a semantic review or absence of bugs. Each entry records
exactly what was checked. Credentials, generated/runtime files and links are
excluded before reading; fixture syntax is intentionally not enforced.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tomllib
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".pi", ".claude", ".audit_state",
    ".next", ".next-build-check", ".next-e2e", "dist", "build", "coverage",
    "test-results", "playwright-report", "var",
})
TEXT_SUFFIXES = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".toml",
    ".yaml", ".yml", ".sh", ".md", ".txt", ".css", ".html", ".svg", ".sql",
    ".ini", ".cfg", ".conf", ".service", ".timer", ".example",
})


def exclusion_reason(path: str) -> str | None:
    parts = PurePosixPath(path).parts
    if not parts or PurePosixPath(path).is_absolute() or ".." in parts:
        return "unsafe_path"
    if parts[0] in {"tmp", "memory"}:
        return "generated_or_local_state"
    if any(part in EXCLUDED_PARTS or part.startswith(".next-") for part in parts):
        return "generated_or_local_state"
    name = parts[-1].lower()
    if ((name.startswith(".env") and name != ".env.example")
            or name in {"id_rsa", "id_ed25519", ".npmrc", ".pypirc"}
            or name.endswith((".pem", ".key", ".p12", ".pfx", ".keystore"))
            or any(part.lower() in {"secrets", "credentials", ".ssh"} for part in parts)):
        return "protected_credentials"
    return None


def repository_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root, check=True, capture_output=True, timeout=30,
    )
    return sorted(set(result.stdout.decode("utf-8").rstrip("\0").split("\0")) - {""})


def python_candidates(source: str, path: str) -> list[dict[str, Any]]:
    """Only report candidates; duplicates in fixtures/tests can be intentional."""
    if any(part in {"tests", "__tests__", "fixtures"} for part in PurePosixPath(path).parts):
        return []
    findings: list[dict[str, Any]] = []
    for node in ast.walk(ast.parse(source, filename=path)):
        if not isinstance(node, ast.Dict):
            continue
        seen: dict[Any, int] = {}
        for key in node.keys:
            if not isinstance(key, ast.Constant):
                continue
            try:
                if key.value in seen:
                    findings.append({"path": path, "line": key.lineno, "kind": "duplicate_literal_dict_key", "first_line": seen[key.value]})
                seen[key.value] = key.lineno
            except TypeError:
                continue
    return findings


def syntax_checks(root: Path, relative: str, data: bytes) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    path = root / relative
    suffix = path.suffix.lower()
    checks = ["full_file_sha256"]
    errors: list[str] = []
    candidates: list[dict[str, Any]] = []
    if suffix not in TEXT_SUFFIXES and path.name not in {"Dockerfile", "Dockerfile.python", "VERSION", "LICENSE", ".gitignore", ".dockerignore"}:
        return checks + ["binary_or_unclassified_not_semantically_reviewed"], errors, candidates
    source = data.decode("utf-8")
    checks.append("utf8_decode")
    if "fixtures" in PurePosixPath(relative).parts:
        return checks + ["fixture_syntax_not_enforced"], errors, candidates
    if suffix == ".py":
        compile(source, relative, "exec", dont_inherit=True)
        checks.append("python_compile_without_execution")
        candidates = python_candidates(source, relative)
    elif suffix == ".json" and path.name.startswith("tsconfig"):
        checks.append("jsonc_requires_separate_typescript_gate")
    elif suffix == ".json":
        json.loads(source)
        checks.append("json_parse")
    elif suffix == ".toml":
        tomllib.loads(source)
        checks.append("toml_parse")
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError:
            checks.append("yaml_parser_unavailable")
        else:
            list(yaml.safe_load_all(source))
            checks.append("yaml_parse")
    elif suffix == ".sh":
        result = subprocess.run(["bash", "-n", "--", str(path)], capture_output=True, timeout=10)
        checks.append("bash_syntax_without_execution")
        if result.returncode:
            # Do not copy source-bearing stderr into reports.
            errors.append(f"bash_syntax_exit_{result.returncode}")
    elif suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        checks.append("requires_separate_typescript_eslint_and_test_gates")
    else:
        checks.append("text_read_not_semantically_reviewed")
    return checks, errors, candidates


def inspect_file(root: Path, relative: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entry: dict[str, Any] = {"path": relative}
    reason = exclusion_reason(relative)
    if reason:
        return {**entry, "status": "excluded", "reason": reason}, []
    path = root / relative
    # Inspect each component without following a link out of the repository.
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            return {**entry, "status": "excluded", "reason": "symlink"}, []
    if not path.is_file():
        return {**entry, "status": "absent", "reason": "deleted_or_nonregular"}, []
    candidates: list[dict[str, Any]] = []
    try:
        data = path.read_bytes()
        entry.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        checks, errors, candidates = syntax_checks(root, relative, data)
        entry.update(checks=checks, errors=errors, status="failed" if errors else "checked")
    except Exception as exc:
        # Record type/line only: exception strings can include file contents.
        entry.update(status="failed", error_type=type(exc).__name__)
        if isinstance(exc, SyntaxError):
            entry["error_line"] = exc.lineno
    return entry, candidates


def build_report(root: Path, paths: list[str], report_path: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for relative in paths:
        if root / relative == report_path:
            continue
        entry, found = inspect_file(root, relative)
        entries.append(entry)
        candidates.extend(found)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "git tracked plus nonignored untracked; no credentials, runtime data, dependencies or links",
        "limitations": [
            "Not an atomic filesystem snapshot; recorded SHA binds each individual read.",
            "Syntax/hash checks are not a manual semantic review or proof that all bugs are fixed.",
            "JavaScript/TypeScript require separate lint, type-check, build and test results.",
            "Binary and fixture contents are hashed, not semantically validated.",
        ],
        "counts": dict(Counter(entry["status"] for entry in entries)),
        "files": entries,
        "review_candidates": candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("var/audit/repository-integrity.json"))
    args = parser.parse_args()
    report_path = (ROOT / args.report).resolve()
    if not report_path.is_relative_to(ROOT):
        parser.error("report must remain inside the repository")
    report = build_report(ROOT, repository_paths(ROOT), report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Repository integrity:", json.dumps(report["counts"], sort_keys=True))
    print("Report:", report_path.relative_to(ROOT))
    for entry in report["files"]:
        if entry["status"] == "failed":
            print("FAILED:", json.dumps(entry, ensure_ascii=False))
    for candidate in report["review_candidates"]:
        print("REVIEW:", json.dumps(candidate, ensure_ascii=False))
    return int(report["counts"].get("failed", 0) > 0)


if __name__ == "__main__":
    raise SystemExit(main())
