#!/usr/bin/env python3
"""Lint the impact-test manifest against the current repository tree."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tomllib
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from test_impact import load_manifest, path_matches  # noqa: E402


DEFAULT_MANIFEST = ROOT / "scripts" / "test-manifest.toml"
IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}
PRODUCTION_PATTERNS = (
    "apps/api/app/**/*.py",
    "apps/worker/app/**/*.py",
    "apps/tgbot/app/**/*.py",
    "image-job/app/**/*.py",
    "packages/core/lumen_core/**/*.py",
    "apps/web/src/**/*.js",
    "apps/web/src/**/*.mjs",
    "apps/web/src/**/*.ts",
    "apps/web/src/**/*.tsx",
)
CRITICAL_DOMAINS = {
    "backend-realtime": (
        "apps/api/app/routes/events.py",
        "apps/api/app/sse_publish.py",
        "apps/api/app/realtime/**",
        "apps/worker/app/sse_publish.py",
        "apps/worker/app/realtime/**",
    ),
    "worker-generation-queue": (
        "apps/worker/app/tasks/generation_parts/queue*.py",
        "apps/worker/app/tasks/generation_parts/lease.py",
        "apps/worker/app/tasks/generation_parts/default_runtime.py",
        "apps/worker/app/tasks/generation_parts/runtime*.py",
    ),
    "worker-upstream-images": (
        "apps/worker/app/upstream_parts/direct_images.py",
        "apps/worker/app/upstream_parts/direct_requests.py",
        "apps/worker/app/upstream_parts/image_dispatch.py",
        "apps/worker/app/upstream_parts/image_race.py",
        "apps/worker/app/upstream_parts/transport.py",
    ),
    "api-stream-assets": (
        "apps/api/app/routes/generations.py",
        "apps/api/app/images/application/http_routes.py",
        "apps/api/app/images/**/*variant*.py",
    ),
    "web-stream-assets": (
        "apps/web/src/components/ui/lightbox/**",
        "apps/web/src/components/ui/stream/**",
        "apps/web/src/components/ui/shell/DesktopStream.tsx",
        "apps/web/src/components/ui/shell/MobileStream.tsx",
        "apps/web/src/lib/queries/stream.ts",
        "apps/web/src/lib/imagePreload.ts",
        "apps/web/src/features/assets/**",
    ),
}
SHELL_SPLIT_RE = re.compile(r"\s*(?:&&|;)\s*")
GLOB_CHARS = frozenset("*?[")


@dataclass(frozen=True)
class Finding:
    code: str
    owner: str
    subject: str
    reason: str


@dataclass(frozen=True)
class AuditReport:
    stale: tuple[Finding, ...]
    unmatched: tuple[Finding, ...]
    shadowed: tuple[Finding, ...]
    critical_fallback_only: tuple[Finding, ...]
    always_full: tuple[Finding, ...]
    production_file_count: int

    @property
    def errors(self) -> tuple[Finding, ...]:
        return self.stale + self.unmatched + self.critical_fallback_only

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "production_file_count": self.production_file_count,
            "stale": [asdict(item) for item in self.stale],
            "unmatched": [asdict(item) for item in self.unmatched],
            "shadowed": [asdict(item) for item in self.shadowed],
            "critical_fallback_only": [
                asdict(item) for item in self.critical_fallback_only
            ],
            "always_full": [asdict(item) for item in self.always_full],
        }


def _normalize_repo_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/") or "."


def _has_glob(value: str) -> bool:
    return any(char in value for char in GLOB_CHARS)


def collect_repository_files(repo_root: Path) -> tuple[str, ...]:
    files: list[str] = []
    for current_root, directory_names, file_names in os.walk(repo_root):
        directory_names[:] = sorted(
            name for name in directory_names if name not in IGNORED_DIRECTORIES
        )
        current = Path(current_root)
        for file_name in sorted(file_names):
            files.append((current / file_name).relative_to(repo_root).as_posix())
    return tuple(files)


def _matching_files(files: Iterable[str], patterns: Sequence[str]) -> set[str]:
    return {
        file_name
        for file_name in files
        if any(path_matches(file_name, pattern) for pattern in patterns)
    }


def _rule_raw_by_name(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rules = raw.get("rules", [])
    return {
        str(rule["name"]): rule
        for rule in rules
        if isinstance(rule, dict) and isinstance(rule.get("name"), str)
    }


def _audit_declared_paths(
    *,
    repo_root: Path,
    files: Sequence[str],
    owner: str,
    patterns: Sequence[str],
    allow_empty_globs: bool,
) -> tuple[list[Finding], list[Finding]]:
    stale: list[Finding] = []
    unmatched: list[Finding] = []
    file_set = set(files)
    for raw_pattern in patterns:
        pattern = _normalize_repo_path(raw_pattern)
        if _has_glob(pattern):
            if not _matching_files(files, (pattern,)) and not allow_empty_globs:
                unmatched.append(
                    Finding(
                        code="empty-glob",
                        owner=owner,
                        subject=pattern,
                        reason="glob does not match any repository file",
                    )
                )
            continue
        if pattern not in file_set and not (repo_root / pattern).exists():
            stale.append(
                Finding(
                    code="missing-literal",
                    owner=owner,
                    subject=pattern,
                    reason="literal manifest path does not exist",
                )
            )
    return stale, unmatched


def _looks_like_test_target(token: str) -> bool:
    normalized = _normalize_repo_path(token.split("::", 1)[0])
    if not normalized or normalized == "." or normalized.startswith("-"):
        return False
    if "$" in normalized or "{" in normalized:
        return False
    parts = Path(normalized).parts
    name = Path(normalized).name.lower()
    return (
        "tests" in parts
        or normalized.startswith("tests/")
        or name.startswith("test_")
        or ".test." in name
    )


def command_test_targets(command: str, *, repo_root: Path) -> tuple[str, ...]:
    current_directory = repo_root
    targets: list[str] = []
    for segment in SHELL_SPLIT_RE.split(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        if not tokens:
            continue
        if tokens[0] == "cd" and len(tokens) >= 2:
            candidate = Path(tokens[1])
            current_directory = (
                candidate if candidate.is_absolute() else repo_root / candidate
            ).resolve()
            continue
        for token in tokens:
            if not _looks_like_test_target(token):
                continue
            candidate_text = token.split("::", 1)[0]
            candidate = Path(candidate_text)
            resolved = (
                candidate if candidate.is_absolute() else current_directory / candidate
            ).resolve()
            try:
                targets.append(resolved.relative_to(repo_root.resolve()).as_posix())
            except ValueError:
                targets.append(resolved.as_posix())
    return tuple(dict.fromkeys(targets))


def _audit_command_targets(
    *,
    repo_root: Path,
    rule_name: str,
    commands: Sequence[str],
) -> list[Finding]:
    stale: list[Finding] = []
    for command in commands:
        for target in command_test_targets(command, repo_root=repo_root):
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = repo_root / candidate
            if not candidate.exists():
                stale.append(
                    Finding(
                        code="missing-test-target",
                        owner=f"rule:{rule_name}",
                        subject=target,
                        reason=f"test command target does not exist: {command}",
                    )
                )
    return stale


def audit_manifest(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    repo_root: Path = ROOT,
    production_patterns: Sequence[str] = PRODUCTION_PATTERNS,
    critical_domains: dict[str, Sequence[str]] = CRITICAL_DOMAINS,
) -> AuditReport:
    manifest_path = manifest_path.resolve()
    repo_root = repo_root.resolve()
    manifest = load_manifest(manifest_path)
    raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    raw_rules = _rule_raw_by_name(raw)
    files = collect_repository_files(repo_root)
    production_files = _matching_files(files, production_patterns)

    stale: list[Finding] = []
    unmatched: list[Finding] = []
    shadowed: list[Finding] = []

    path_stale, path_unmatched = _audit_declared_paths(
        repo_root=repo_root,
        files=files,
        owner="planner.full_mandatory_paths",
        patterns=manifest.full_mandatory_paths,
        allow_empty_globs=False,
    )
    stale.extend(path_stale)
    unmatched.extend(path_unmatched)

    specific_coverage: set[str] = set()
    fallback_coverage: set[str] = set()
    rule_matches: dict[str, set[str]] = {}
    for rule in manifest.rules:
        raw_rule = raw_rules.get(rule.name, {})
        allow_empty = raw_rule.get("allow_empty", False)
        if not isinstance(allow_empty, bool):
            raise ValueError(f"rules.{rule.name}.allow_empty must be a boolean")
        path_stale, path_unmatched = _audit_declared_paths(
            repo_root=repo_root,
            files=files,
            owner=f"rule:{rule.name}",
            patterns=rule.paths,
            allow_empty_globs=allow_empty,
        )
        stale.extend(path_stale)
        unmatched.extend(path_unmatched)
        matches = _matching_files(production_files, rule.paths)
        rule_matches[rule.name] = matches
        if rule.fallback:
            fallback_coverage.update(matches)
        else:
            specific_coverage.update(matches)
            stale.extend(
                _audit_command_targets(
                    repo_root=repo_root,
                    rule_name=rule.name,
                    commands=rule.commands,
                )
            )

    for file_name in sorted(production_files - specific_coverage - fallback_coverage):
        unmatched.append(
            Finding(
                code="uncovered-production-file",
                owner="production-coverage",
                subject=file_name,
                reason="production file is not covered by any manifest rule",
            )
        )

    for rule in manifest.rules:
        if not rule.fallback:
            continue
        matches = rule_matches[rule.name]
        if matches and matches <= specific_coverage:
            shadowed.append(
                Finding(
                    code="shadowed-fallback",
                    owner=f"rule:{rule.name}",
                    subject=rule.name,
                    reason=(
                        f"all {len(matches)} matching production files are already "
                        "covered by non-fallback rules"
                    ),
                )
            )

    critical_fallback_only: list[Finding] = []
    for domain_name, patterns in sorted(critical_domains.items()):
        domain_files = _matching_files(production_files, patterns)
        for file_name in sorted(domain_files - specific_coverage):
            fallback_rules = sorted(
                rule.name
                for rule in manifest.rules
                if rule.fallback and file_name in rule_matches[rule.name]
            )
            reason = "high-risk production file has no non-fallback rule"
            if fallback_rules:
                reason += f"; fallback only: {', '.join(fallback_rules)}"
            critical_fallback_only.append(
                Finding(
                    code="critical-domain-fallback-only",
                    owner=f"critical-domain:{domain_name}",
                    subject=file_name,
                    reason=reason,
                )
            )

    always_full: list[Finding] = []
    for pattern in manifest.full_mandatory_paths:
        matches = sorted(_matching_files(files, (pattern,)))
        if not matches and not _has_glob(pattern) and (repo_root / pattern).exists():
            matches = [pattern]
        sample = ", ".join(matches[:3]) if matches else "no current matches"
        if len(matches) > 3:
            sample += f", +{len(matches) - 3} more"
        always_full.append(
            Finding(
                code="full-mandatory-path",
                owner="planner.full_mandatory_paths",
                subject=pattern,
                reason=f"{len(matches)} matching path(s): {sample}",
            )
        )

    def finding_key(item: Finding) -> tuple[str, str, str, str]:
        return item.owner, item.subject, item.code, item.reason

    return AuditReport(
        stale=tuple(sorted(set(stale), key=finding_key)),
        unmatched=tuple(sorted(set(unmatched), key=finding_key)),
        shadowed=tuple(sorted(set(shadowed), key=finding_key)),
        critical_fallback_only=tuple(
            sorted(set(critical_fallback_only), key=finding_key)
        ),
        always_full=tuple(sorted(set(always_full), key=finding_key)),
        production_file_count=len(production_files),
    )


def render_report(report: AuditReport) -> str:
    lines = [
        f"manifest lint: {'PASS' if report.ok else 'FAIL'}",
        f"production files: {report.production_file_count}",
    ]
    sections = (
        ("stale", report.stale),
        ("unmatched", report.unmatched),
        ("critical_fallback_only", report.critical_fallback_only),
        ("shadowed", report.shadowed),
        ("always_full", report.always_full),
    )
    for name, findings in sections:
        lines.append(f"{name}: {len(findings)}")
        for finding in findings:
            lines.append(
                f"  - [{finding.code}] {finding.owner}: "
                f"{finding.subject} ({finding.reason})"
            )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = audit_manifest(args.manifest, repo_root=args.repo_root)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print(f"manifest lint configuration error: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(render_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
