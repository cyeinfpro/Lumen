#!/usr/bin/env python3
"""Generate Lumen's governance score from repository and test evidence."""

from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = ROOT / ".audit_state" / "governance-evidence.json"
DEFAULT_JSON_OUTPUT = ROOT / "docs" / "refactors" / "governance-score.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "docs" / "refactors" / "governance-score.md"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
WORKTREE_STATUS_COMMAND = (
    "git",
    "status",
    "--porcelain=v1",
    "--untracked-files=all",
)
WEB_TEST_SUFFIXES = frozenset(
    {".cjs", ".cts", ".js", ".jsx", ".mjs", ".mts", ".ts", ".tsx"}
)
KNOWN_DEFECT_SEVERITIES = frozenset({"P0", "P1", "P2", "P3"})
KNOWN_DEFECT_STATUSES = frozenset({"open", "closed"})
JS_TEST_MODIFIERS = frozenset(
    {"concurrent", "each", "failing", "fails", "only", "skip", "todo"}
)
JS_DISABLED_TEST_MODIFIERS = frozenset({"failing", "fails", "only", "skip", "todo"})
PYTHON_DISABLED_TEST_MARKERS = frozenset(
    {
        "expected_failure",
        "expectedfailure",
        "skip",
        "skipif",
        "skipunless",
        "xfail",
    }
)

WEIGHTS = {
    "funding_async_correctness": 0.15,
    "runtime_ownership": 0.12,
    "module_boundaries": 0.12,
    "ci_and_gates": 0.12,
    "release_update_rollback": 0.10,
    "web_state_isolation": 0.10,
    "data_migration_storage": 0.08,
    "observability_recovery": 0.08,
    "security_supply_chain": 0.06,
    "debt_documentation": 0.07,
}

DIMENSION_CHECKS = {
    "funding_async_correctness": (
        "known_p0_zero",
        "known_p1_zero",
        "sidecar_recovery_faults",
        "sidecar_delivery_faults",
    ),
    "runtime_ownership": (
        "runtime_gate",
        "ownership_registry_complete",
        "runtime_scanner_full_roots",
    ),
    "module_boundaries": (
        "architecture_gate",
        "architecture_layers_valid",
        "architecture_layers_consumed",
        "web_domain_boundaries",
        "billing_dynamic_facade_zero",
    ),
    "ci_and_gates": (
        "manifest_gate",
        "rerun_plan_identity",
        "baseline_monotonic",
        "full_tests",
    ),
    "release_update_rollback": (
        "migration_gate",
        "release_tag_main_guard",
        "release_before_stable_alias",
        "updater_health_commit",
        "release_faults",
        "release_proof",
    ),
    "web_state_isolation": (
        "web_p1_zero",
        "web_isolation",
        "web_domain_boundaries",
    ),
    "data_migration_storage": (
        "migration_gate",
        "migration_faults",
        "storage_consistency",
    ),
    "observability_recovery": (
        "fault_matrix",
        "observability_metrics",
        "recovery_proof",
    ),
    "security_supply_chain": (
        "release_tag_main_guard",
        "signed_images",
        "supply_chain",
    ),
    "debt_documentation": (
        "dead_code_zero",
        "facade_gate",
        "facade_inventory",
        "documentation_freshness",
    ),
}

HARD_GATES = (
    "known_p0_zero",
    "known_p1_zero",
    "manifest_gate",
    "runtime_gate",
    "ownership_registry_complete",
    "migration_gate",
    "release_tag_main_guard",
    "updater_health_commit",
    "web_isolation",
    "full_tests",
    "release_proof",
    "worktree_clean",
)

STATIC_COMMANDS = {
    "architecture_gate": ("uv", "run", "python", "scripts/check_architecture.py"),
    "baseline_monotonic": (
        "uv",
        "run",
        "python",
        "scripts/baseline_monotonic.py",
    ),
    "complexity_gate": ("uv", "run", "python", "scripts/check_complexity.py"),
    "dead_code_zero": ("uv", "run", "python", "scripts/dead_code_audit.py"),
    "facade_gate": ("uv", "run", "python", "scripts/architecture_audit.py"),
    "facade_inventory": (
        "uv",
        "run",
        "python",
        "scripts/facade_inventory.py",
    ),
    "manifest_gate": ("uv", "run", "python", "scripts/test_manifest_lint.py"),
    "migration_gate": (
        "uv",
        "run",
        "python",
        "scripts/lint_alembic_breaking.py",
        "--base",
        "HEAD^",
        "--head",
        "HEAD",
    ),
    "runtime_gate": (
        "uv",
        "run",
        "python",
        "scripts/module_runtime_state_audit.py",
    ),
}


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    source: str
    detail: str


@dataclass(frozen=True)
class _JsToken:
    kind: str
    value: str
    offset: int


Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _head_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or COMMIT_RE.fullmatch(commit) is None:
        raise ValueError("cannot resolve the current git commit")
    return commit


def _command_checks(root: Path, runner: Runner) -> dict[str, CheckResult]:
    checks: dict[str, CheckResult] = {}
    for name, command in STATIC_COMMANDS.items():
        result = runner(command, root)
        output = (result.stdout or result.stderr).strip().splitlines()
        checks[name] = CheckResult(
            passed=result.returncode == 0,
            source="command",
            detail=output[-1] if output else f"exit={result.returncode}",
        )
    return checks


def _fixed_commit_is_ancestor(root: Path, commit: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode in {0, 1}:
        return result.returncode == 0
    detail = (result.stderr or result.stdout).strip()
    raise ValueError(
        f"cannot verify fixed commit ancestry for {commit}: "
        f"{detail or f'exit={result.returncode}'}"
    )


def _worktree_check(root: Path, runner: Runner) -> CheckResult:
    result = runner(WORKTREE_STATUS_COMMAND, root)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return CheckResult(
            False,
            "git",
            f"cannot verify worktree state: {detail or f'exit={result.returncode}'}",
        )
    dirty_paths = [line for line in (result.stdout or "").splitlines() if line.strip()]
    return CheckResult(
        passed=not dirty_paths,
        source="git",
        detail=(
            "tracked and untracked source tree matches HEAD"
            if not dirty_paths
            else (
                f"{len(dirty_paths)} dirty path(s); commit-bound evidence "
                "requires a clean worktree"
            )
        ),
    )


def _read_test_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read regression test {path}: {exc}") from exc


def _split_pytest_selector(selector: str) -> list[str]:
    parts: list[str] = []
    start = 0
    bracket_depth = 0
    index = 0
    while index < len(selector):
        char = selector[index]
        if char == "[":
            bracket_depth += 1
        elif char == "]":
            if bracket_depth == 0:
                raise ValueError(f"invalid pytest selector {selector!r}: unmatched ']'")
            bracket_depth -= 1
        elif selector.startswith("::", index) and bracket_depth == 0:
            parts.append(selector[start:index])
            index += 2
            start = index
            continue
        index += 1
    if bracket_depth:
        raise ValueError(f"invalid pytest selector {selector!r}: unmatched '['")
    parts.append(selector[start:])
    names = [part.split("[", 1)[0] for part in parts]
    if any(not name for name in names):
        raise ValueError(f"invalid pytest selector {selector!r}: empty node id")
    return names


def _python_expression_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        return _python_expression_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _python_expression_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _python_marker_is_disabled(node: ast.AST) -> bool:
    for candidate in ast.walk(node):
        name = _python_expression_name(candidate)
        if (
            name is not None
            and name.rsplit(".", 1)[-1].lower() in PYTHON_DISABLED_TEST_MARKERS
        ):
            return True
    return False


def _python_scope_is_disabled(body: list[ast.stmt]) -> bool:
    for statement in body:
        value: ast.AST | None = None
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            value = statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == "pytestmark"
        ):
            value = statement.value
        if value is None:
            continue
        markers = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        if any(_python_marker_is_disabled(marker) for marker in markers):
            return True
    return False


def _python_node_is_disabled(
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    return any(_python_marker_is_disabled(item) for item in node.decorator_list)


def _python_test_reference_exists(path: Path, selector: str) -> bool:
    source = _read_test_source(path)
    try:
        body = ast.parse(source, filename=str(path)).body
    except SyntaxError as exc:
        location = f"line {exc.lineno}" if exc.lineno else "unknown line"
        raise ValueError(
            f"cannot parse Python regression test {path}: {exc.msg} ({location})"
        ) from exc
    if _python_scope_is_disabled(body):
        return False
    names = _split_pytest_selector(selector)
    for index, part in enumerate(names):
        node = next(
            (
                candidate
                for candidate in body
                if isinstance(
                    candidate,
                    (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
                )
                and candidate.name == part
            ),
            None,
        )
        if node is None:
            return False
        if _python_node_is_disabled(node):
            return False
        if index == len(names) - 1:
            return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if not isinstance(node, ast.ClassDef):
            return False
        body = node.body
        if _python_scope_is_disabled(body):
            return False
    return False


def _js_parse_error(
    path: Path,
    source: str,
    offset: int,
    message: str,
) -> ValueError:
    line = source.count("\n", 0, offset) + 1
    return ValueError(
        f"cannot parse JavaScript regression test {path}: {message} at line {line}"
    )


def _consume_js_escape(
    source: str,
    index: int,
    *,
    path: Path,
) -> tuple[str, int]:
    escaped_index = index + 1
    if escaped_index >= len(source):
        raise _js_parse_error(path, source, index, "unterminated escape")
    escaped = source[escaped_index]
    if escaped in "\r\n":
        if escaped == "\r" and source.startswith("\r\n", escaped_index):
            return "", escaped_index + 2
        return "", escaped_index + 1
    simple = {
        "0": "\0",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
    }
    if escaped in simple:
        return simple[escaped], escaped_index + 1
    if escaped == "x":
        digits = source[escaped_index + 1 : escaped_index + 3]
        if len(digits) != 2 or any(
            char not in "0123456789abcdefABCDEF" for char in digits
        ):
            raise _js_parse_error(path, source, index, "invalid hexadecimal escape")
        return chr(int(digits, 16)), escaped_index + 3
    if escaped == "u":
        digits_start = escaped_index + 1
        if digits_start < len(source) and source[digits_start] == "{":
            closing = source.find("}", digits_start + 1)
            if closing < 0:
                raise _js_parse_error(
                    path, source, index, "unterminated Unicode escape"
                )
            digits = source[digits_start + 1 : closing]
            if (
                not 1 <= len(digits) <= 6
                or any(char not in "0123456789abcdefABCDEF" for char in digits)
                or int(digits, 16) > 0x10FFFF
            ):
                raise _js_parse_error(path, source, index, "invalid Unicode escape")
            return chr(int(digits, 16)), closing + 1
        digits = source[digits_start : digits_start + 4]
        if len(digits) != 4 or any(
            char not in "0123456789abcdefABCDEF" for char in digits
        ):
            raise _js_parse_error(path, source, index, "invalid Unicode escape")
        return chr(int(digits, 16)), digits_start + 4
    return escaped, escaped_index + 1


def _consume_js_quoted_string(
    source: str,
    start: int,
    *,
    path: Path,
) -> tuple[str, int]:
    quote = source[start]
    value: list[str] = []
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == quote:
            return "".join(value), index + 1
        if char in "\r\n":
            raise _js_parse_error(path, source, start, "unterminated string literal")
        if char == "\\":
            decoded, index = _consume_js_escape(source, index, path=path)
            value.append(decoded)
            continue
        value.append(char)
        index += 1
    raise _js_parse_error(path, source, start, "unterminated string literal")


def _js_regex_can_start(previous: _JsToken | None) -> bool:
    if previous is None:
        return True
    if previous.kind in {"number", "regex", "string", "template"}:
        return False
    if previous.kind == "identifier":
        return previous.value in {
            "await",
            "case",
            "delete",
            "do",
            "else",
            "in",
            "instanceof",
            "of",
            "return",
            "throw",
            "typeof",
            "void",
            "yield",
        }
    return previous.value not in {")", "]", "}", "<"}


def _consume_js_regex(
    source: str,
    start: int,
    *,
    path: Path,
) -> int:
    index = start + 1
    in_character_class = False
    while index < len(source):
        char = source[index]
        if char in "\r\n":
            raise _js_parse_error(
                path, source, start, "unterminated regular expression"
            )
        if char == "\\":
            index += 2
            continue
        if char == "[":
            in_character_class = True
        elif char == "]":
            in_character_class = False
        elif char == "/" and not in_character_class:
            index += 1
            while index < len(source) and (
                source[index].isalnum() or source[index] in "$_"
            ):
                index += 1
            return index
        index += 1
    raise _js_parse_error(path, source, start, "unterminated regular expression")


def _consume_js_block_comment(
    source: str,
    start: int,
    *,
    path: Path,
) -> int:
    closing = source.find("*/", start + 2)
    if closing < 0:
        raise _js_parse_error(path, source, start, "unterminated block comment")
    return closing + 2


def _consume_js_template_expression(
    source: str,
    start: int,
    *,
    path: Path,
) -> int:
    opening_for = {")": "(", "]": "[", "}": "{"}
    stack = ["{"]
    previous: _JsToken | None = None
    index = start
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            index = _consume_js_block_comment(source, index, path=path)
            continue
        if char in {"'", '"'}:
            value, index = _consume_js_quoted_string(source, index, path=path)
            previous = _JsToken("string", value, index)
            continue
        if char == "`":
            kind, value, index = _consume_js_template(source, index, path=path)
            previous = _JsToken(kind, value, index)
            continue
        if char == "/" and _js_regex_can_start(previous):
            index = _consume_js_regex(source, index, path=path)
            previous = _JsToken("regex", "", index)
            continue
        if char.isalpha() or char in "$_":
            end = index + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "$_"):
                end += 1
            previous = _JsToken("identifier", source[index:end], index)
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "._"):
                end += 1
            previous = _JsToken("number", source[index:end], index)
            index = end
            continue
        if char in "([{":
            stack.append(char)
        elif char in ")]}":
            expected = opening_for[char]
            if not stack or stack[-1] != expected:
                raise _js_parse_error(path, source, index, f"unmatched {char!r}")
            stack.pop()
            if not stack:
                return index + 1
        previous = _JsToken("punctuation", char, index)
        index += 1
    raise _js_parse_error(path, source, start, "unterminated template expression")


def _consume_js_template(
    source: str,
    start: int,
    *,
    path: Path,
) -> tuple[str, str, int]:
    value: list[str] = []
    dynamic = False
    index = start + 1
    while index < len(source):
        char = source[index]
        if char == "`":
            return (
                "dynamic_template" if dynamic else "template",
                "" if dynamic else "".join(value),
                index + 1,
            )
        if char == "\\":
            decoded, index = _consume_js_escape(source, index, path=path)
            if not dynamic:
                value.append(decoded)
            continue
        if source.startswith("${", index):
            dynamic = True
            index = _consume_js_template_expression(source, index + 2, path=path)
            continue
        if not dynamic:
            value.append(char)
        index += 1
    raise _js_parse_error(path, source, start, "unterminated template literal")


def _tokenize_javascript(source: str, *, path: Path) -> list[_JsToken]:
    opening_for = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    tokens: list[_JsToken] = []
    index = 0
    if source.startswith("#!"):
        newline = source.find("\n")
        index = len(source) if newline < 0 else newline + 1
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            index = _consume_js_block_comment(source, index, path=path)
            continue
        if char in {"'", '"'}:
            value, end = _consume_js_quoted_string(source, index, path=path)
            tokens.append(_JsToken("string", value, index))
            index = end
            continue
        if char == "`":
            kind, value, end = _consume_js_template(source, index, path=path)
            tokens.append(_JsToken(kind, value, index))
            index = end
            continue
        previous = tokens[-1] if tokens else None
        if char == "/" and _js_regex_can_start(previous):
            end = _consume_js_regex(source, index, path=path)
            tokens.append(_JsToken("regex", source[index:end], index))
            index = end
            continue
        if char.isalpha() or char in "$_":
            end = index + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "$_"):
                end += 1
            tokens.append(_JsToken("identifier", source[index:end], index))
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "._"):
                end += 1
            tokens.append(_JsToken("number", source[index:end], index))
            index = end
            continue
        if char in "([{":
            stack.append((char, index))
        elif char in ")]}":
            expected = opening_for[char]
            if not stack or stack[-1][0] != expected:
                raise _js_parse_error(path, source, index, f"unmatched {char!r}")
            stack.pop()
        tokens.append(_JsToken("punctuation", char, index))
        index += 1
    if stack:
        opening, offset = stack[-1]
        raise _js_parse_error(path, source, offset, f"unclosed {opening!r}")
    return tokens


def _after_js_call(tokens: list[_JsToken], opening_index: int) -> int | None:
    depth = 0
    for index in range(opening_index, len(tokens)):
        value = tokens[index].value
        if value == "(":
            depth += 1
        elif value == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _web_test_names(path: Path) -> set[str]:
    source = _read_test_source(path)
    tokens = _tokenize_javascript(source, path=path)
    names: set[str] = set()
    for index, token in enumerate(tokens):
        if token.kind != "identifier" or token.value not in {"it", "test"}:
            continue
        if index > 0 and tokens[index - 1].value == ".":
            continue
        cursor = index + 1
        modifiers: list[str] = []
        while (
            cursor + 1 < len(tokens)
            and tokens[cursor].value == "."
            and tokens[cursor + 1].kind == "identifier"
            and tokens[cursor + 1].value in JS_TEST_MODIFIERS
        ):
            modifiers.append(tokens[cursor + 1].value)
            cursor += 2
        if "each" in modifiers:
            if cursor < len(tokens) and tokens[cursor].value == "(":
                after_data = _after_js_call(tokens, cursor)
                if after_data is None:
                    continue
                cursor = after_data
            elif cursor < len(tokens) and tokens[cursor].kind in {
                "dynamic_template",
                "template",
            }:
                cursor += 1
            else:
                continue
            while (
                cursor + 1 < len(tokens)
                and tokens[cursor].value == "."
                and tokens[cursor + 1].kind == "identifier"
                and tokens[cursor + 1].value in JS_DISABLED_TEST_MODIFIERS
            ):
                modifiers.append(tokens[cursor + 1].value)
                cursor += 2
        if (
            cursor + 1 < len(tokens)
            and tokens[cursor].value == "("
            and tokens[cursor + 1].kind in {"string", "template"}
            and not JS_DISABLED_TEST_MODIFIERS.intersection(modifiers)
        ):
            names.add(tokens[cursor + 1].value)
    return names


def _web_test_reference_exists(path: Path, test_name: str) -> bool:
    return test_name in _web_test_names(path)


def _regression_test_reference_exists(root: Path, reference: str) -> bool:
    relative_path, separator, selector = reference.partition("::")
    if not separator or not relative_path or not selector:
        raise ValueError(
            "regression test reference must be '<relative-path>::<selector>'"
        )
    relative = Path(relative_path)
    if relative.is_absolute():
        raise ValueError(f"regression test path must be relative: {relative_path!r}")
    try:
        resolved_root = root.resolve()
        path = (resolved_root / relative).resolve()
        path.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"regression test path escapes repository root: {relative_path!r}"
        ) from exc
    if not path.is_file():
        return False
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _python_test_reference_exists(path, selector)
    if suffix in WEB_TEST_SUFFIXES:
        return _web_test_reference_exists(path, selector)
    raise ValueError(
        f"unsupported regression test suffix {suffix or '<none>'!r} "
        f"for {relative_path!r}"
    )


def _registry_string(
    entry: dict[str, Any],
    field: str,
    *,
    context: str,
) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} field {field!r} must be a non-empty string")
    return value


def _registry_string_list(
    entry: dict[str, Any],
    field: str,
    *,
    context: str,
    required: bool,
) -> list[str] | None:
    if field not in entry and not required:
        return None
    value = entry.get(field)
    if not isinstance(value, list) or (required and not value):
        requirement = "a non-empty list" if required else "a list"
        raise ValueError(f"{context} field {field!r} must be {requirement}")
    strings: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"{context} field {field}[{index}] must be a non-empty string"
            )
        strings.append(item)
    return strings


def _known_defect_checks(root: Path) -> dict[str, CheckResult]:
    payload = _load_json(root / "docs/refactors/known-defects.json")
    defects = payload.get("defects")
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise ValueError("known-defects registry field 'version' must be integer 1")
    if not isinstance(defects, list):
        raise ValueError("known-defects registry field 'defects' must be a list")
    open_by_severity: dict[str, list[str]] = {"P0": [], "P1": []}
    web_open: list[str] = []
    verified_commits: dict[str, bool] = {}
    seen_ids: set[str] = set()
    for index, entry in enumerate(defects):
        if not isinstance(entry, dict):
            raise ValueError(f"known-defects entry {index} must be an object")
        entry_context = f"known-defects entry {index}"
        defect_id = _registry_string(entry, "id", context=entry_context)
        context = f"known defect {defect_id}"
        if defect_id in seen_ids:
            raise ValueError(f"duplicate known defect id {defect_id!r}")
        seen_ids.add(defect_id)
        severity = _registry_string(entry, "severity", context=context)
        if severity not in KNOWN_DEFECT_SEVERITIES:
            allowed = ", ".join(sorted(KNOWN_DEFECT_SEVERITIES))
            raise ValueError(f"{context} field 'severity' must be one of: {allowed}")
        status = _registry_string(entry, "status", context=context)
        if status not in KNOWN_DEFECT_STATUSES:
            allowed = ", ".join(sorted(KNOWN_DEFECT_STATUSES))
            raise ValueError(f"{context} field 'status' must be one of: {allowed}")
        for field in ("owner", "summary"):
            if field in entry:
                _registry_string(entry, field, context=context)
        _registry_string_list(
            entry,
            "paths",
            context=context,
            required=False,
        )
        tests = _registry_string_list(
            entry,
            "regression_tests",
            context=context,
            required=status == "closed",
        )
        fixed_commit = entry.get("fixed_commit")
        if fixed_commit is not None and (
            not isinstance(fixed_commit, str)
            or COMMIT_RE.fullmatch(fixed_commit) is None
        ):
            raise ValueError(
                f"{context} field 'fixed_commit' must be a 40-character "
                "lowercase git commit"
            )
        if status == "closed":
            if fixed_commit is None:
                raise ValueError(
                    f"{context} field 'fixed_commit' is required when closed"
                )
            assert tests is not None
            missing_tests: list[str] = []
            for test in tests:
                try:
                    exists = _regression_test_reference_exists(root, test)
                except ValueError as exc:
                    raise ValueError(
                        f"{context} has invalid regression test {test!r}: {exc}"
                    ) from exc
                if not exists:
                    missing_tests.append(test)
            if missing_tests:
                raise ValueError(
                    f"{context} references missing regression tests: "
                    f"{', '.join(missing_tests)}"
                )
            if fixed_commit not in verified_commits:
                verified_commits[fixed_commit] = _fixed_commit_is_ancestor(
                    root, fixed_commit
                )
            if not verified_commits[fixed_commit]:
                raise ValueError(f"{context} fixed commit is not an ancestor of HEAD")
        elif severity in open_by_severity:
            open_by_severity[severity].append(defect_id)
        if defect_id in {"P1-05", "P1-06", "P1-07"} and status != "closed":
            web_open.append(defect_id)
    return {
        "known_p0_zero": CheckResult(
            passed=not open_by_severity["P0"],
            source="known-defects",
            detail=",".join(open_by_severity["P0"]) or "0 open P0",
        ),
        "known_p1_zero": CheckResult(
            passed=not open_by_severity["P1"],
            source="known-defects",
            detail=",".join(open_by_severity["P1"]) or "0 open P1",
        ),
        "web_p1_zero": CheckResult(
            passed=not web_open,
            source="known-defects",
            detail=",".join(web_open) or "web P1 defects closed",
        ),
    }


def _ownership_check(root: Path) -> CheckResult:
    ownership = _load_json(root / "docs/refactors/module-ownership.json")
    runtime = _load_json(root / "docs/refactors/module-runtime-state-ledger.json")
    modules = ownership.get("modules")
    runtime_modules = runtime.get("modules")
    if (
        ownership.get("version") != 1
        or not isinstance(modules, list)
        or not isinstance(runtime_modules, list)
    ):
        raise ValueError("unsupported module ownership registry")
    required = {
        "path",
        "owner",
        "composition_root",
        "shutdown",
        "test_reset",
        "symbols",
    }
    registered: dict[str, dict[str, Any]] = {}
    for entry in modules:
        if not isinstance(entry, dict) or not required.issubset(entry):
            raise ValueError("invalid module ownership entry")
        registered[str(entry["path"])] = entry
    missing: list[str] = []
    for runtime_entry in runtime_modules:
        if not isinstance(runtime_entry, dict):
            raise ValueError("invalid runtime ledger entry")
        path = str(runtime_entry.get("path", ""))
        owner_entry = registered.get(path)
        if owner_entry is None:
            missing.append(path)
            continue
        expected = set(runtime_entry.get("symbols") or [])
        actual = set(owner_entry.get("symbols") or [])
        if expected != actual:
            missing.append(f"{path}:symbol-mismatch")
    return CheckResult(
        passed=not missing,
        source="module-ownership",
        detail=",".join(missing) or f"{len(registered)} modules registered",
    )


def _workflow_job_block(workflow: str, job_name: str) -> str | None:
    match = re.search(
        rf"^  {re.escape(job_name)}:\s*$",
        workflow,
        flags=re.MULTILINE,
    )
    if match is None:
        return None
    next_job = re.search(
        r"^  [A-Za-z0-9_-]+:\s*$",
        workflow[match.end() :],
        flags=re.MULTILINE,
    )
    end = match.end() + next_job.start() if next_job is not None else len(workflow)
    return workflow[match.start() : end]


def _workflow_job_needs(job: str | None) -> frozenset[str] | None:
    if job is None:
        return None
    match = re.search(r"^    needs:\s*\[([^\]\n]+)\]\s*$", job, re.MULTILINE)
    if match is None:
        return None
    dependencies = frozenset(
        dependency.strip()
        for dependency in match.group(1).split(",")
        if dependency.strip()
    )
    return dependencies or None


def _workflow_job_condition(job: str | None) -> str:
    if job is None:
        return ""
    match = re.search(r"^    if:\s*(.+?)\s*$", job, re.MULTILINE)
    return match.group(1) if match is not None else ""


def _release_publication_dag_check(workflow: str) -> CheckResult:
    release_job = _workflow_job_block(workflow, "release")
    shared_job = _workflow_job_block(workflow, "promote-shared")
    release_needs = _workflow_job_needs(release_job)
    shared_needs = _workflow_job_needs(shared_job)
    shared_condition = _workflow_job_condition(shared_job)
    release_defers_latest = bool(
        release_job is not None
        and re.search(r"^\s+make_latest:\s*false\s*$", release_job, re.MULTILINE)
    )
    shared_alias_position = (
        shared_job.find("--phase mutable") if shared_job is not None else -1
    )
    shared_latest_position = (
        shared_job.find("gh release edit") if shared_job is not None else -1
    )
    passed = (
        release_needs == frozenset({"resolve-ref", "promote"})
        and shared_needs == frozenset({"resolve-ref", "promote", "release"})
        and "needs.resolve-ref.outputs.is_release == 'true'" in shared_condition
        and "needs.promote.outputs.is_prerelease == 'false'" in shared_condition
        and "always()" not in shared_condition
        and release_defers_latest
        and 0 <= shared_alias_position < shared_latest_position
        and "--latest" in (shared_job or "")
    )
    if passed:
        detail = (
            "GitHub Release/manifest gates stable aliases; GitHub latest moves "
            "only after aliases; prereleases skip shared aliases"
        )
    else:
        detail = (
            "release/shared alias DAG mismatch: "
            f"release_needs={sorted(release_needs or ())}, "
            f"shared_needs={sorted(shared_needs or ())}, "
            f"shared_if={shared_condition!r}, "
            f"release_defers_latest={release_defers_latest}, "
            f"shared_alias_position={shared_alias_position}, "
            f"shared_latest_position={shared_latest_position}"
        )
    return CheckResult(passed, "source", detail)


def _source_checks(root: Path) -> dict[str, CheckResult]:
    architecture = (root / "scripts/check_architecture.py").read_text(encoding="utf-8")
    runtime = (root / "scripts/module_runtime_state_audit.py").read_text(
        encoding="utf-8"
    )
    test_runner = (root / "scripts/run_test_plan.py").read_text(encoding="utf-8")
    release = (root / ".github/workflows/docker-release.yml").read_text(
        encoding="utf-8"
    )
    updater_health = (root / "scripts/update/services/health.sh").read_text(
        encoding="utf-8"
    )
    billing_surface = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((root / "apps/api/app/routes/billing_parts").glob("*.py"))
    )
    layers_path = root / "scripts/architecture-layers.toml"
    layers_valid = False
    if layers_path.is_file():
        layers = tomllib.loads(layers_path.read_text(encoding="utf-8"))
        layers_valid = (
            layers.get("version") == 1
            and isinstance(layers.get("packages"), list)
            and isinstance(layers.get("rules"), list)
        )
    checks = {
        "architecture_layers_valid": CheckResult(
            layers_valid,
            "source",
            "scripts/architecture-layers.toml",
        ),
        "architecture_layers_consumed": CheckResult(
            "architecture-layers.toml" in architecture,
            "source",
            "check_architecture.py consumes declarative layers",
        ),
        "runtime_scanner_full_roots": CheckResult(
            '"packages" / "core" / "lumen_core"' in runtime
            and '"apps" / "tgbot" / "app"' in runtime,
            "source",
            "runtime scanner covers Core and TgBot",
        ),
        "rerun_plan_identity": CheckResult(
            "plan_identity" in test_runner
            and "commands that were never executed" in test_runner,
            "source",
            "rerun results bind current plan and command set",
        ),
        "release_tag_main_guard": CheckResult(
            "git merge-base --is-ancestor" in release and "origin/main" in release,
            "source",
            "release tag ancestry guard",
        ),
        "release_before_stable_alias": _release_publication_dag_check(release),
        "updater_health_commit": CheckResult(
            updater_health.find("mark_update_committed")
            < updater_health.find("emit_done health_check 0")
            and updater_health.find("mark_update_committed") >= 0,
            "source",
            "commit marker follows health proof",
        ),
        "billing_dynamic_facade_zero": CheckResult(
            all(
                marker not in billing_surface
                for marker in ("globals()", "ContextVar[Any]", "current_runtime")
            ),
            "source",
            "billing dynamic facade markers absent",
        ),
    }
    return checks


def _evidence_checks(
    path: Path,
    *,
    commit: str,
    root: Path = ROOT,
) -> dict[str, CheckResult]:
    if not path.is_file():
        return {}
    payload = _load_json(path)
    if payload.get("version") != 1 or payload.get("commit") != commit:
        raise ValueError("governance evidence is stale or unsupported")
    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, dict):
        raise ValueError("governance evidence checks must be an object")
    expected_commands = _expected_evidence_commands(root)
    checks: dict[str, CheckResult] = {}
    for name, entry in raw_checks.items():
        if not isinstance(name, str) or name not in expected_commands:
            raise ValueError(f"unknown governance evidence check: {name}")
        if not isinstance(entry, dict):
            raise ValueError(f"invalid governance evidence check: {name}")
        expected_command = expected_commands[name]
        command = entry.get("command")
        expected_digest = hashlib.sha256(expected_command.encode("utf-8")).hexdigest()
        actual_digest = (
            hashlib.sha256(command.encode("utf-8")).hexdigest()
            if isinstance(command, str)
            else ""
        )
        passed = (
            entry.get("status") == "passed"
            and entry.get("exit_code") == 0
            and actual_digest == expected_digest
        )
        detail = (
            expected_command
            if actual_digest == expected_digest
            else f"command digest mismatch; expected sha256={expected_digest}"
        )
        checks[name] = CheckResult(
            passed=passed,
            source="evidence",
            detail=detail,
        )
    return checks


def _expected_evidence_commands(root: Path) -> dict[str, str]:
    path = root / "scripts" / "governance_evidence.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise ValueError(f"cannot load governance evidence commands: {exc}") from exc
    raw_commands: object | None = None
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CHECK_COMMANDS"
            for target in statement.targets
        ):
            raw_commands = ast.literal_eval(statement.value)
            break
    if not isinstance(raw_commands, dict):
        raise ValueError("governance evidence CHECK_COMMANDS must be a literal mapping")
    commands: dict[str, str] = {}
    for name, parts in raw_commands.items():
        if (
            not isinstance(name, str)
            or not isinstance(parts, (list, tuple))
            or not parts
            or any(not isinstance(part, str) or not part for part in parts)
        ):
            raise ValueError("invalid governance evidence CHECK_COMMANDS entry")
        commands[name] = " && ".join(parts)
    return commands


def _merge_check_result(
    current: CheckResult | None,
    candidate: CheckResult,
) -> CheckResult:
    if current is None:
        return candidate
    if not current.passed:
        return current
    if not candidate.passed:
        return candidate
    return CheckResult(
        passed=True,
        source=f"{current.source}+{candidate.source}",
        detail=f"{current.detail}; {candidate.detail}",
    )


def build_report(
    *,
    root: Path = ROOT,
    evidence_path: Path = DEFAULT_EVIDENCE,
    runner: Runner = _run,
    generated_at: str | None = None,
) -> dict[str, Any]:
    commit = _head_commit(root)
    checks: dict[str, CheckResult] = {}
    checks.update(_command_checks(root, runner))
    checks.update(_known_defect_checks(root))
    checks["ownership_registry_complete"] = _ownership_check(root)
    checks.update(_source_checks(root))
    for name, evidence in _evidence_checks(
        evidence_path,
        commit=commit,
        root=root,
    ).items():
        checks[name] = _merge_check_result(checks.get(name), evidence)
    checks["worktree_clean"] = _worktree_check(root, runner)

    all_check_names = {
        name for names in DIMENSION_CHECKS.values() for name in names
    } | set(HARD_GATES)
    for name in sorted(all_check_names):
        checks.setdefault(
            name,
            CheckResult(False, "missing", "required evidence is missing"),
        )

    dimensions: dict[str, dict[str, Any]] = {}
    weighted_score = 0.0
    for dimension, weight in WEIGHTS.items():
        names = DIMENSION_CHECKS[dimension]
        passed = sum(1 for name in names if checks[name].passed)
        score = 10.0 * passed / len(names)
        contribution = score * weight
        weighted_score += contribution
        dimensions[dimension] = {
            "checks": list(names),
            "passed": passed,
            "score": round(score, 3),
            "total": len(names),
            "weight": weight,
            "weighted_contribution": round(contribution, 3),
        }

    hard_gate_results = {name: checks[name].passed for name in HARD_GATES}
    hard_gates_passed = all(hard_gate_results.values())
    return {
        "checks": {name: asdict(result) for name, result in sorted(checks.items())},
        "commit": commit,
        "dimensions": dimensions,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "hard_gate_results": hard_gate_results,
        "hard_gates_passed": hard_gates_passed,
        "schema_version": 1,
        "status": (
            "passed" if hard_gates_passed and weighted_score >= 9.0 else "not_achieved"
        ),
        "weighted_score": round(weighted_score, 3),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Lumen Governance Score",
        "",
        f"- Commit: `{report['commit']}`",
        f"- Generated: `{report['generated_at']}`",
        f"- Weighted score: **{report['weighted_score']:.3f}/10**",
        f"- Hard gates: **{'passed' if report['hard_gates_passed'] else 'failed'}**",
        f"- Status: **{report['status']}**",
        "",
        "| Dimension | Weight | Passed | Score | Contribution |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, dimension in report["dimensions"].items():
        lines.append(
            f"| `{name}` | {dimension['weight']:.0%} | "
            f"{dimension['passed']}/{dimension['total']} | "
            f"{dimension['score']:.3f} | "
            f"{dimension['weighted_contribution']:.3f} |"
        )
    lines.extend(["", "## Failed Hard Gates", ""])
    failed = [
        name for name, passed in report["hard_gate_results"].items() if not passed
    ]
    lines.extend(f"- `{name}`" for name in failed)
    if not failed:
        lines.append("- None")
    lines.extend(["", "## Missing Or Failed Evidence", ""])
    missing = [
        (name, value) for name, value in report["checks"].items() if not value["passed"]
    ]
    lines.extend(
        f"- `{name}`: {value['detail']} ({value['source']})" for name, value in missing
    )
    if not missing:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=DEFAULT_MARKDOWN_OUTPUT,
    )
    parser.add_argument(
        "--require-passed",
        action="store_true",
        help="return non-zero unless hard gates pass and score is at least 9.0",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(evidence_path=args.evidence)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.markdown_output.write_text(
        render_markdown(report),
        encoding="utf-8",
    )
    print(
        f"governance score={report['weighted_score']:.3f} "
        f"hard_gates={'passed' if report['hard_gates_passed'] else 'failed'} "
        f"status={report['status']}"
    )
    if args.require_passed and report["status"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
