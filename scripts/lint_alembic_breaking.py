#!/usr/bin/env python3
"""Reject Alembic operations that are unsafe for blue/green deploys.

The linter only inspects upgrade() bodies. Downgrade paths are allowed to drop
objects because they are not executed during a rolling update.
"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Callable
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


BREAKING_METHODS = {
    "drop_column": "drop column",
    "drop_table": "drop table",
    "rename_column": "rename column",
    "rename_table": "rename table",
}
ALEMBIC_VERSION_NUM_MAX = 32
MIGRATION_PATH_GLOB = "apps/api/alembic/versions/*.py"
SAFE_RAW_DML_RE = re.compile(
    r"^(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b",
    re.IGNORECASE,
)
SAFE_VALIDATE_CONSTRAINT_RE = re.compile(
    r"^ALTER\s+TABLE\s+(?:ONLY\s+)?[\s\S]+?\s+VALIDATE\s+CONSTRAINT\s+[\s\S]+$",
    re.IGNORECASE,
)
SQLALCHEMY_EXECUTABLE_METHODS = {"delete", "insert", "select", "update"}
SQLALCHEMY_EXECUTABLE_CHAIN_METHODS = {
    "cte",
    "execution_options",
    "filter",
    "filter_by",
    "from_select",
    "group_by",
    "having",
    "join",
    "limit",
    "offset",
    "options",
    "order_by",
    "returning",
    "values",
    "where",
}


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    message: str


@dataclass(frozen=True)
class MigrationChange:
    status: str
    old_path: Path | None
    new_path: Path | None


@dataclass(frozen=True)
class ExecutionStatement:
    raw_sql: str | None = None
    sqlalchemy_executable: bool = False


class BaselineError(RuntimeError):
    """Raised when changed migrations cannot be determined safely."""


def _is_migration_path(path: Path | None) -> bool:
    return path is not None and path.match(MIGRATION_PATH_GLOB)


def _call_name(node: ast.Call, operation_aliases: set[str]) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id in operation_aliases:
            return func.attr
    return None


def _assigned_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.List, ast.Tuple)):
        return {name for element in target.elts for name in _assigned_names(element)}
    return set()


def _register_operation_import(
    node: ast.Import | ast.ImportFrom,
    operation_aliases: set[str],
) -> None:
    if isinstance(node, ast.ImportFrom):
        if node.module != "alembic":
            return
        for imported_name in node.names:
            if imported_name.name == "op":
                operation_aliases.add(imported_name.asname or imported_name.name)
        return

    for imported_name in node.names:
        if imported_name.name == "alembic.op" and imported_name.asname:
            operation_aliases.add(imported_name.asname)


def _is_operation_receiver(node: ast.AST, operation_aliases: set[str]) -> bool:
    return isinstance(node, ast.Name) and node.id in operation_aliases


def _operation_method_name(
    node: ast.AST,
    operation_aliases: set[str],
) -> str | None:
    if isinstance(node, ast.Attribute) and _is_operation_receiver(
        node.value, operation_aliases
    ):
        return node.attr
    return None


def _operation_aliases(tree: ast.AST) -> set[str]:
    aliases = {"op"}
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _register_operation_import(node, aliases)
            continue
        if isinstance(node, ast.Assign):
            names = {
                name for target in node.targets for name in _assigned_names(target)
            }
            is_operation_receiver = _is_operation_receiver(node.value, aliases)
            aliases.difference_update(names)
            if is_operation_receiver:
                aliases.update(names)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            names = _assigned_names(node.target)
            is_operation_receiver = _is_operation_receiver(node.value, aliases)
            aliases.difference_update(names)
            if is_operation_receiver:
                aliases.update(names)
    return aliases


def _literal_bool(node: ast.AST | None) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _literal_none(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _string_template(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{}")
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _string_template(node.left)
        right = _string_template(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _text_call_argument(node: ast.Call) -> ast.AST | None:
    if node.args and (
        (isinstance(node.func, ast.Name) and node.func.id == "text")
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "text")
    ):
        return node.args[0]
    return None


def _static_raw_sql(
    node: ast.AST,
    statement_aliases: dict[str, ExecutionStatement],
) -> str | None:
    if isinstance(node, ast.Name):
        statement = statement_aliases.get(node.id)
        return statement.raw_sql if statement is not None else None

    template = _string_template(node)
    if template is not None:
        return template

    if not isinstance(node, ast.Call):
        return None
    text_argument = _text_call_argument(node)
    if text_argument is not None:
        return _static_raw_sql(text_argument, statement_aliases)
    if isinstance(node.func, ast.Attribute) and node.func.attr in {
        "bindparams",
        "columns",
        "execution_options",
    }:
        return _static_raw_sql(node.func.value, statement_aliases)
    return None


def _is_sqlalchemy_executable(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id in SQLALCHEMY_EXECUTABLE_METHODS
    if not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr in SQLALCHEMY_EXECUTABLE_METHODS:
        return True
    return (
        node.func.attr in SQLALCHEMY_EXECUTABLE_CHAIN_METHODS
        and _is_sqlalchemy_executable(node.func.value)
    )


def _classify_execution_statement(
    node: ast.AST,
    statement_aliases: dict[str, ExecutionStatement],
) -> ExecutionStatement | None:
    if isinstance(node, ast.Name):
        return statement_aliases.get(node.id)
    raw_sql = _static_raw_sql(node, statement_aliases)
    if raw_sql is not None:
        return ExecutionStatement(raw_sql=raw_sql)
    if _is_sqlalchemy_executable(node):
        return ExecutionStatement(sqlalchemy_executable=True)
    return None


def _is_safe_raw_sql(sql: str) -> bool:
    statement = sql.strip()
    if statement.endswith(";"):
        statement = statement[:-1].rstrip()
    if not statement or ";" in statement:
        return False
    normalized = " ".join(statement.split())
    return bool(
        SAFE_RAW_DML_RE.match(normalized)
        or SAFE_VALIDATE_CONSTRAINT_RE.fullmatch(normalized)
    )


def _function_parameters(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[str]:
    return [
        argument.arg
        for argument in (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
    ]


class UpgradeVisitor(ast.NodeVisitor):
    def __init__(
        self,
        path: Path,
        operation_aliases: set[str],
        helper_functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    ) -> None:
        self.path = path
        self.violations: list[Violation] = []
        self.operation_aliases = set(operation_aliases)
        self.batch_context_aliases: set[str] = set()
        self.connection_aliases: set[str] = set()
        self.operation_method_aliases: dict[str, str] = {}
        self.raw_execution_aliases: set[str] = set()
        self.statement_aliases: dict[str, ExecutionStatement] = {}
        self.helper_functions = dict(helper_functions)
        self.active_helper_ids: set[int] = set()
        self.helper_depth = 0

    def _is_batch_context(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Name) and node.id in self.batch_context_aliases
        ) or (
            isinstance(node, ast.Call)
            and _call_name(node, self.operation_aliases) == "batch_alter_table"
        )

    def _is_connection_receiver(self, node: ast.AST) -> bool:
        return (
            _is_operation_receiver(node, self.operation_aliases)
            or (
                isinstance(node, ast.Call)
                and self._operation_call_name(node) == "get_bind"
            )
            or (isinstance(node, ast.Name) and node.id in self.connection_aliases)
        )

    def _is_raw_execution_receiver(self, node: ast.AST) -> bool:
        return self.helper_depth > 0 or self._is_connection_receiver(node)

    def _is_raw_execution_callable(self, node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "execute"
            and self._is_raw_execution_receiver(node.value)
        )

    def _forget_names(self, names: set[str]) -> None:
        self.operation_aliases.difference_update(names)
        self.batch_context_aliases.difference_update(names)
        self.connection_aliases.difference_update(names)
        self.raw_execution_aliases.difference_update(names)
        for name in names:
            self.operation_method_aliases.pop(name, None)
            self.statement_aliases.pop(name, None)
            self.helper_functions.pop(name, None)

    def _bind_names(self, names: set[str], value: ast.AST) -> None:
        if not names:
            return
        is_operation_receiver = _is_operation_receiver(
            value,
            self.operation_aliases,
        )
        is_batch_context = self._is_batch_context(value)
        is_connection_receiver = self._is_connection_receiver(value)
        operation_method = _operation_method_name(value, self.operation_aliases)
        raw_execution_callable = self._is_raw_execution_callable(value)
        helper_function = (
            self.helper_functions.get(value.id) if isinstance(value, ast.Name) else None
        )
        statement = _classify_execution_statement(value, self.statement_aliases)
        self._forget_names(names)
        if is_operation_receiver:
            self.operation_aliases.update(names)
        elif is_batch_context:
            self.batch_context_aliases.update(names)
        elif is_connection_receiver:
            self.connection_aliases.update(names)
        elif operation_method is not None:
            self.operation_method_aliases.update(
                {name: operation_method for name in names}
            )
        elif helper_function is not None:
            self.helper_functions.update({name: helper_function for name in names})
        if raw_execution_callable:
            self.raw_execution_aliases.update(names)
        if statement is not None:
            self.statement_aliases.update({name: statement for name in names})

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        _register_operation_import(node, self.operation_aliases)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        _register_operation_import(node, self.operation_aliases)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        self.visit(node.value)
        self._bind_names(
            {name for target in node.targets for name in _assigned_names(target)},
            node.value,
        )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if node.value is None:
            return
        self.visit(node.value)
        self._bind_names(_assigned_names(node.target), node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:  # noqa: N802
        self.visit(node.value)
        self._bind_names(_assigned_names(node.target), node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._forget_names({node.name})
        self.helper_functions[node.name] = node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._forget_names({node.name})
        self.helper_functions[node.name] = node

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        previous_state = self._scope_state()
        self._clone_scope_state()
        try:
            for item in node.items:
                self.visit(item.context_expr)
                names = (
                    _assigned_names(item.optional_vars)
                    if item.optional_vars is not None
                    else set()
                )
                self._forget_names(names)
                if self._is_batch_context(item.context_expr):
                    self.operation_aliases.update(names)
            for statement in node.body:
                self.visit(statement)
        finally:
            self._restore_scope_state(previous_state)

    def visit_With(self, node: ast.With) -> None:  # noqa: N802
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802
        self._visit_with(node)

    def _scope_state(
        self,
    ) -> tuple[
        set[str],
        set[str],
        set[str],
        dict[str, str],
        set[str],
        dict[str, ExecutionStatement],
        dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    ]:
        return (
            self.operation_aliases,
            self.batch_context_aliases,
            self.connection_aliases,
            self.operation_method_aliases,
            self.raw_execution_aliases,
            self.statement_aliases,
            self.helper_functions,
        )

    def _clone_scope_state(self) -> None:
        self.operation_aliases = set(self.operation_aliases)
        self.batch_context_aliases = set(self.batch_context_aliases)
        self.connection_aliases = set(self.connection_aliases)
        self.operation_method_aliases = dict(self.operation_method_aliases)
        self.raw_execution_aliases = set(self.raw_execution_aliases)
        self.statement_aliases = dict(self.statement_aliases)
        self.helper_functions = dict(self.helper_functions)

    def _restore_scope_state(
        self,
        state: tuple[
            set[str],
            set[str],
            set[str],
            dict[str, str],
            set[str],
            dict[str, ExecutionStatement],
            dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
        ],
    ) -> None:
        (
            self.operation_aliases,
            self.batch_context_aliases,
            self.connection_aliases,
            self.operation_method_aliases,
            self.raw_execution_aliases,
            self.statement_aliases,
            self.helper_functions,
        ) = state

    def _report_raw_execution(self, node: ast.Call) -> None:
        statement_node = node.args[0] if node.args else _keyword(node, "sqltext")
        statement = (
            _classify_execution_statement(statement_node, self.statement_aliases)
            if statement_node is not None
            else None
        )
        if statement is None:
            message = "raw SQL passed to Alembic execution cannot be classified safely"
        elif statement.sqlalchemy_executable:
            return
        elif statement.raw_sql is not None and _is_safe_raw_sql(statement.raw_sql):
            return
        else:
            message = (
                "raw SQL DDL via Alembic execution is not an approved expand operation"
            )
        self.violations.append(Violation(self.path, node.lineno, message))

    def _visit_helper(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        call: ast.Call,
    ) -> None:
        helper_id = id(node)
        if helper_id in self.active_helper_ids:
            self.violations.append(
                Violation(
                    self.path,
                    call.lineno,
                    "recursive migration helper cannot be analyzed safely",
                )
            )
            return
        previous_state = self._scope_state()
        self._clone_scope_state()
        self.active_helper_ids.add(helper_id)
        self.helper_depth += 1
        try:
            parameters = _function_parameters(node)
            self._forget_names(set(parameters))
            for parameter, argument in zip(parameters, call.args, strict=False):
                self._bind_names({parameter}, argument)
            for keyword in call.keywords:
                if keyword.arg is not None and keyword.arg in parameters:
                    self._bind_names({keyword.arg}, keyword.value)
            for statement in node.body:
                self.visit(statement)
        finally:
            self.helper_depth -= 1
            self.active_helper_ids.remove(helper_id)
            self._restore_scope_state(previous_state)

    def _operation_call_name(self, node: ast.Call) -> str | None:
        name = _call_name(node, self.operation_aliases)
        if name is not None:
            return name
        if isinstance(node.func, ast.Name):
            return self.operation_method_aliases.get(node.func.id)
        return None

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        name = self._operation_call_name(node)
        if name in BREAKING_METHODS:
            self.violations.append(
                Violation(
                    self.path,
                    node.lineno,
                    f"{name} is a breaking {BREAKING_METHODS[name]} operation",
                )
            )
        elif name == "alter_column":
            nullable = _literal_bool(_keyword(node, "nullable"))
            server_default = _keyword(node, "server_default")
            if nullable is False and (
                server_default is None or _literal_none(server_default)
            ):
                self.violations.append(
                    Violation(
                        self.path,
                        node.lineno,
                        "alter_column(nullable=False) must set a server_default "
                        "or be split into expand/contract",
                    )
                )
        elif name == "create_check_constraint":
            not_valid = _literal_bool(_keyword(node, "postgresql_not_valid"))
            if not_valid is not True:
                self.violations.append(
                    Violation(
                        self.path,
                        node.lineno,
                        "create_check_constraint must use a NOT VALID/VALIDATE "
                        "pattern for rolling deploys",
                    )
                )
        raw_execution = (
            name == "execute"
            or (
                isinstance(node.func, ast.Name)
                and node.func.id in self.raw_execution_aliases
            )
            or self._is_raw_execution_callable(node.func)
        )
        if raw_execution:
            self._report_raw_execution(node)
        if isinstance(node.func, ast.Name):
            helper = self.helper_functions.get(node.func.id)
            if helper is not None:
                self._visit_helper(helper, node)
        self.generic_visit(node)


def _upgrade_functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "upgrade"
    ]


def _module_helpers(
    tree: ast.AST,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in getattr(tree, "body", [])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name != "upgrade"
    }


def _module_string_assignment(tree: ast.AST, name: str) -> tuple[str, int] | None:
    for node in getattr(tree, "body", []):
        value: ast.AST | None = None
        lineno = getattr(node, "lineno", 1)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                value = node.value
        elif isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            ):
                value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value, lineno
    return None


def _module_value_fingerprint(
    source: str,
    *,
    path: Path,
    name: str,
) -> tuple[str | None, int]:
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise BaselineError(
            f"cannot parse {path} while comparing {name}: {exc.msg}"
        ) from exc
    for node in getattr(tree, "body", []):
        value: ast.AST | None = None
        line = getattr(node, "lineno", 1)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                value = node.value
        elif isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            ):
                value = node.value
        if value is not None:
            return ast.dump(value, include_attributes=False), line
    return None, 1


def lint_file(path: Path) -> list[Violation]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 1, f"syntax error: {exc.msg}")]
    out: list[Violation] = []
    revision = _module_string_assignment(tree, "revision")
    if revision is not None:
        revision_id, line = revision
        if len(revision_id) > ALEMBIC_VERSION_NUM_MAX:
            out.append(
                Violation(
                    path,
                    line,
                    "revision id exceeds alembic_version.version_num "
                    f"VARCHAR({ALEMBIC_VERSION_NUM_MAX})",
                )
            )
    operation_aliases = _operation_aliases(tree)
    helper_functions = _module_helpers(tree)
    for fn in _upgrade_functions(tree):
        visitor = UpgradeVisitor(path, operation_aliases, helper_functions)
        for stmt in fn.body:
            visitor.visit(stmt)
        out.extend(visitor.violations)
    return out


def _git_output_bytes(command: list[str]) -> bytes:
    result = subprocess.run(
        command,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (
            (result.stderr or result.stdout)
            .decode(
                "utf-8",
                errors="replace",
            )
            .strip()
        )
        raise BaselineError(
            f"{' '.join(command)} failed: {detail or f'exit={result.returncode}'}"
        )
    return result.stdout


def _parse_name_status(raw: bytes) -> list[MigrationChange]:
    fields = raw.decode("utf-8", errors="surrogateescape").split("\0")
    if fields and not fields[-1]:
        fields.pop()
    changes: list[MigrationChange] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if not status:
            continue
        kind = status[0]
        if kind in {"R", "C"}:
            if index + 1 >= len(fields):
                raise BaselineError("malformed git diff --name-status output")
            changes.append(
                MigrationChange(
                    status=kind,
                    old_path=Path(fields[index]),
                    new_path=Path(fields[index + 1]),
                )
            )
            index += 2
            continue
        if index >= len(fields):
            raise BaselineError("malformed git diff --name-status output")
        path = Path(fields[index])
        index += 1
        changes.append(
            MigrationChange(
                status=kind,
                old_path=path if kind != "A" else None,
                new_path=path if kind != "D" else None,
            )
        )
    return changes


def _migration_changes_from_name_status(raw: bytes) -> list[MigrationChange]:
    return [
        change
        for change in _parse_name_status(raw)
        if _is_migration_path(change.old_path) or _is_migration_path(change.new_path)
    ]


def _git_migration_changes(*, base: str, head: str) -> list[MigrationChange]:
    raw = _git_output_bytes(
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--diff-filter=ACMRD",
            base,
            head,
        ]
    )
    return _migration_changes_from_name_status(raw)


def _changed_files(changes: list[MigrationChange]) -> list[Path]:
    return sorted(
        {change.new_path for change in changes if _is_migration_path(change.new_path)}
    )


def _git_changed_files(*, base: str, head: str) -> list[Path]:
    return _changed_files(_git_migration_changes(base=base, head=head))


def _git_ref_exists(ref: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _working_tree_migration_changes() -> list[MigrationChange]:
    changes: list[MigrationChange] = []
    if _git_ref_exists("HEAD"):
        changes.extend(
            _migration_changes_from_name_status(
                _git_output_bytes(
                    [
                        "git",
                        "diff",
                        "--name-status",
                        "-z",
                        "--find-renames",
                        "--diff-filter=ACMRD",
                        "HEAD",
                    ]
                )
            )
        )
    raw_untracked = _git_output_bytes(
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "apps/api/alembic/versions",
        ]
    )
    changes.extend(
        MigrationChange(status="A", old_path=None, new_path=Path(path))
        for path in raw_untracked.decode("utf-8", errors="surrogateescape").split("\0")
        if path and _is_migration_path(Path(path))
    )
    return changes


def _git_file(ref: str, path: Path) -> str:
    return _git_output_bytes(["git", "show", f"{ref}:{path.as_posix()}"]).decode(
        "utf-8", errors="surrogateescape"
    )


def _historical_revision_violations(
    changes: list[MigrationChange],
    *,
    read_before: Callable[[Path], str] | None = None,
    read_after: Callable[[Path], str] | None = None,
) -> list[Violation]:
    violations: list[Violation] = []
    for change in changes:
        if change.status == "D" and _is_migration_path(change.old_path):
            assert change.old_path is not None
            violations.append(
                Violation(
                    change.old_path,
                    1,
                    "existing Alembic revision was deleted; historical "
                    "migration files are immutable",
                )
            )
            continue
        if change.status == "R" and _is_migration_path(change.old_path):
            assert change.old_path is not None
            display_path = (
                change.new_path
                if _is_migration_path(change.new_path)
                else change.old_path
            )
            assert display_path is not None
            violations.append(
                Violation(
                    display_path,
                    1,
                    "existing Alembic revision was moved; historical migration "
                    "paths are immutable",
                )
            )
            continue
        if (
            change.status != "M"
            or not _is_migration_path(change.old_path)
            or not _is_migration_path(change.new_path)
        ):
            continue
        assert change.old_path is not None
        assert change.new_path is not None
        if read_before is None or read_after is None:
            raise BaselineError("cannot compare a modified historical Alembic revision")
        before_source = read_before(change.old_path)
        after_source = read_after(change.new_path)
        if before_source == after_source:
            continue
        before_fingerprint, _ = _module_value_fingerprint(
            before_source,
            path=change.old_path,
            name="down_revision",
        )
        after_fingerprint, line = _module_value_fingerprint(
            after_source,
            path=change.new_path,
            name="down_revision",
        )
        if before_fingerprint != after_fingerprint:
            violations.append(
                Violation(
                    change.new_path,
                    line,
                    "down_revision of an existing Alembic revision was "
                    "rewritten; the historical migration graph is immutable",
                )
            )
            continue
        violations.append(
            Violation(
                change.new_path,
                1,
                "existing Alembic revision content was changed; historical "
                "migration files are immutable",
            )
        )
    return violations


def _git_history_violations(
    changes: list[MigrationChange],
    *,
    base: str,
    head: str,
) -> list[Violation]:
    return _historical_revision_violations(
        changes,
        read_before=lambda path: _git_file(base, path),
        read_after=lambda path: _git_file(head, path),
    )


def _working_tree_history_violations(
    changes: list[MigrationChange],
) -> list[Violation]:
    if not any(change.status == "M" for change in changes):
        return _historical_revision_violations(changes)
    if not _git_ref_exists("HEAD"):
        raise BaselineError(
            "cannot compare a modified historical migration without HEAD"
        )
    return _historical_revision_violations(
        changes,
        read_before=lambda path: _git_file("HEAD", path),
        read_after=lambda path: path.read_text(encoding="utf-8"),
    )


def _has_breaking_marker(message_file: str | None) -> bool:
    if os.environ.get("LUMEN_ALLOW_BREAKING_ALEMBIC") == "1":
        return True
    if not message_file:
        return False
    try:
        text = Path(message_file).read_text(encoding="utf-8")
    except OSError:
        return False
    return "BREAKING:" in text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument("--allow-breaking", action="store_true")
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--commit-message-file")
    args = parser.parse_args(argv)

    if args.files and (args.base or args.head):
        parser.error("positional files cannot be combined with --base/--head")
    if bool(args.base) != bool(args.head):
        parser.error("--base and --head must be provided together")

    try:
        if args.files:
            files = args.files
            historical_violations: list[Violation] = []
        elif args.base and args.head:
            changes = _git_migration_changes(base=args.base, head=args.head)
            files = _changed_files(changes)
            historical_violations = _git_history_violations(
                changes,
                base=args.base,
                head=args.head,
            )
        else:
            changes = _working_tree_migration_changes()
            if not changes:
                raise BaselineError(
                    "no changed migrations found and no explicit --base/--head "
                    "comparison was provided"
                )
            files = _changed_files(changes)
            historical_violations = _working_tree_history_violations(changes)
    except BaselineError as exc:
        print(f"Cannot determine Alembic migration baseline: {exc}", file=sys.stderr)
        return 2

    files = [path for path in files if path.exists() and _is_migration_path(path)]
    violations = historical_violations + [
        violation for path in files for violation in lint_file(path)
    ]
    if not violations:
        return 0
    if args.allow_breaking or _has_breaking_marker(args.commit_message_file):
        print(
            "Alembic breaking operations acknowledged by BREAKING marker:",
            file=sys.stderr,
        )
        for item in violations:
            print(f"  {item.path}:{item.line}: {item.message}", file=sys.stderr)
        return 0
    print("Unsafe Alembic migration operations found:", file=sys.stderr)
    for item in violations:
        print(f"  {item.path}:{item.line}: {item.message}", file=sys.stderr)
    print(
        "Add a BREAKING: runbook note only for planned downtime migrations.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
