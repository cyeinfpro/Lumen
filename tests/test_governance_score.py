from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = spec_from_file_location(
    "lumen_governance_score",
    ROOT / "scripts" / "governance_score.py",
)
assert SPEC is not None and SPEC.loader is not None
governance_score = module_from_spec(SPEC)
sys.modules[SPEC.name] = governance_score
SPEC.loader.exec_module(governance_score)


def _runner(
    command: list[str] | tuple[str, ...],
    _cwd: Path,
) -> subprocess.CompletedProcess[str]:
    if tuple(command) == governance_score.WORKTREE_STATUS_COMMAND:
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.CompletedProcess(command, 0, "passed\n", "")


def _dirty_runner(
    command: list[str] | tuple[str, ...],
    _cwd: Path,
) -> subprocess.CompletedProcess[str]:
    if tuple(command) == governance_score.WORKTREE_STATUS_COMMAND:
        return subprocess.CompletedProcess(
            command,
            0,
            " M scripts/governance_score.py\n",
            "",
        )
    return subprocess.CompletedProcess(command, 0, "passed\n", "")


def _write_registry(
    root: Path,
    defects: list[object],
    *,
    version: object = 1,
) -> Path:
    registry = root / "docs" / "refactors" / "known-defects.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps({"version": version, "defects": defects}),
        encoding="utf-8",
    )
    return registry


def _closed_defect(
    reference: object,
    *,
    fixed_commit: object = "f" * 40,
    **overrides: object,
) -> dict[str, object]:
    entry: dict[str, object] = {
        "fixed_commit": fixed_commit,
        "id": "P1-test",
        "regression_tests": [reference],
        "severity": "P1",
        "status": "closed",
    }
    entry.update(overrides)
    return entry


def _write_evidence(
    path: Path,
    *,
    commit: str,
    check_names: set[str],
) -> None:
    expected_commands = governance_score._expected_evidence_commands(ROOT)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "commit": commit,
                "checks": {
                    name: {
                        "command": expected_commands[name],
                        "exit_code": 0,
                        "status": "passed",
                    }
                    for name in check_names
                    if name in expected_commands
                },
            }
        ),
        encoding="utf-8",
    )


def test_current_report_fails_closed_without_dynamic_evidence() -> None:
    report = governance_score.build_report(
        root=ROOT,
        evidence_path=ROOT / ".audit_state/missing-governance-evidence.json",
        runner=_runner,
        generated_at="2026-07-29T00:00:00+00:00",
    )

    assert report["status"] == "not_achieved"
    assert report["hard_gates_passed"] is False
    assert report["checks"]["full_tests"]["passed"] is False
    assert report["checks"]["known_p0_zero"]["passed"] is True
    assert report["checks"]["known_p1_zero"]["passed"] is True
    assert report["checks"]["worktree_clean"]["passed"] is True
    assert report["weighted_score"] < 9.0


def test_head_bound_evidence_cannot_pass_on_dirty_worktree(
    tmp_path: Path,
) -> None:
    commit = governance_score._head_commit(ROOT)
    evidence = tmp_path / "evidence.json"
    check_names = {
        name for names in governance_score.DIMENSION_CHECKS.values() for name in names
    } | set(governance_score.HARD_GATES)
    _write_evidence(evidence, commit=commit, check_names=check_names)

    report = governance_score.build_report(
        root=ROOT,
        evidence_path=evidence,
        runner=_dirty_runner,
        generated_at="2026-07-30T00:00:00+00:00",
    )

    assert report["weighted_score"] == 10.0
    assert report["status"] == "not_achieved"
    assert report["hard_gates_passed"] is False
    assert report["hard_gate_results"]["worktree_clean"] is False
    assert [
        name for name, passed in report["hard_gate_results"].items() if not passed
    ] == ["worktree_clean"]
    assert (
        "commit-bound evidence requires a clean worktree"
        in (report["checks"]["worktree_clean"]["detail"])
    )


def test_worktree_status_failure_is_a_failed_gate() -> None:
    def failing_runner(
        command: list[str] | tuple[str, ...],
        _cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 128, "", "not a git tree")

    check = governance_score._worktree_check(ROOT, failing_runner)

    assert check.passed is False
    assert "cannot verify worktree state" in check.detail
    assert "not a git tree" in check.detail


def test_current_known_defects_reference_real_tests_and_commits() -> None:
    checks = governance_score._known_defect_checks(ROOT)

    assert checks["known_p0_zero"].passed is True
    assert checks["known_p1_zero"].passed is True


def test_current_head_is_accepted_as_fixed_commit_ancestor() -> None:
    commit = governance_score._head_commit(ROOT)

    assert governance_score._fixed_commit_is_ancestor(ROOT, commit) is True


def test_closed_defect_rejects_missing_regression_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_registry(
        tmp_path,
        [_closed_defect("tests/test_missing.py::test_missing")],
    )
    monkeypatch.setattr(
        governance_score,
        "_fixed_commit_is_ancestor",
        lambda _root, _commit: True,
    )

    with pytest.raises(ValueError, match="missing regression tests"):
        governance_score._known_defect_checks(tmp_path)


def test_closed_defect_rejects_unreachable_fixed_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_file = tmp_path / "tests" / "test_registered.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_registered():\n    pass\n", encoding="utf-8")
    _write_registry(
        tmp_path,
        [_closed_defect("tests/test_registered.py::test_registered")],
    )
    monkeypatch.setattr(
        governance_score,
        "_fixed_commit_is_ancestor",
        lambda _root, _commit: False,
    )

    with pytest.raises(ValueError, match="not an ancestor of HEAD"):
        governance_score._known_defect_checks(tmp_path)


def test_parametrized_python_selector_preserves_double_colons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_file = tmp_path / "tests" / "test_param.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "def test_param(value=None):\n    assert value is None\n",
        encoding="utf-8",
    )
    _write_registry(
        tmp_path,
        [_closed_defect("tests/test_param.py::test_param[value::part]")],
    )
    monkeypatch.setattr(
        governance_score,
        "_fixed_commit_is_ancestor",
        lambda _root, _commit: True,
    )

    checks = governance_score._known_defect_checks(tmp_path)

    assert checks["known_p1_zero"].passed is True


def test_python_regression_test_syntax_error_is_actionable(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "tests" / "test_broken.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_broken(:\n    pass\n", encoding="utf-8")
    _write_registry(
        tmp_path,
        [_closed_defect("tests/test_broken.py::test_broken")],
    )

    with pytest.raises(
        ValueError,
        match=(
            "known defect P1-test has invalid regression test .*"
            "cannot parse Python regression test"
        ),
    ):
        governance_score._known_defect_checks(tmp_path)


def test_javascript_test_modifiers_are_detected_without_lexical_decoys(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "tests" / "registry.test.ts"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        """
// test("comment decoy", () => {});
const stringDecoy = 'test("string decoy", () => {})';
const templateDecoy = `test("template decoy", () => {})`;
const regexDecoy = /test\\("regex decoy"/;

test.skip("skipped case", () => {});
test.only("only case", () => {});
test.each([[1], [2]])("table case", (value) => value);
it.each`value ${1}`("tagged table case", (value) => value);
test.skip.each([[1]])("skipped table case", (value) => value);
test.each([[1]]).only("only table case", (value) => value);
test.each([[1]]).failing("failing table case", (value) => value);
""",
        encoding="utf-8",
    )

    for name in {"table case", "tagged table case"}:
        assert governance_score._regression_test_reference_exists(
            tmp_path,
            f"tests/registry.test.ts::{name}",
        )
    for decoy in {
        "comment decoy",
        "failing table case",
        "only case",
        "only table case",
        "regex decoy",
        "skipped case",
        "skipped table case",
        "string decoy",
        "template decoy",
    }:
        assert not governance_score._regression_test_reference_exists(
            tmp_path,
            f"tests/registry.test.ts::{decoy}",
        )


def test_python_disabled_regression_tests_are_rejected(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_disabled.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        """
import pytest

@pytest.mark.skip(reason="disabled")
def test_skipped():
    pass

@pytest.mark.xfail(reason="known failure")
def test_expected_failure():
    pass

def test_enabled():
    pass
""",
        encoding="utf-8",
    )

    assert not governance_score._regression_test_reference_exists(
        tmp_path,
        "tests/test_disabled.py::test_skipped",
    )
    assert not governance_score._regression_test_reference_exists(
        tmp_path,
        "tests/test_disabled.py::test_expected_failure",
    )
    assert governance_score._regression_test_reference_exists(
        tmp_path,
        "tests/test_disabled.py::test_enabled",
    )


def test_class_and_module_level_disabled_tests_are_rejected(tmp_path: Path) -> None:
    class_file = tmp_path / "tests" / "test_disabled_class.py"
    class_file.parent.mkdir(parents=True)
    class_file.write_text(
        """
import pytest

@pytest.mark.skip(reason="disabled class")
class TestDisabled:
    def test_member(self):
        pass
""",
        encoding="utf-8",
    )
    module_file = tmp_path / "tests" / "test_disabled_module.py"
    module_file.write_text(
        """
import pytest

pytestmark = pytest.mark.xfail(reason="disabled module")

def test_member():
    pass
""",
        encoding="utf-8",
    )

    assert not governance_score._regression_test_reference_exists(
        tmp_path,
        "tests/test_disabled_class.py::TestDisabled::test_member",
    )
    assert not governance_score._regression_test_reference_exists(
        tmp_path,
        "tests/test_disabled_module.py::test_member",
    )


def test_parametrized_xfail_or_skip_marks_are_rejected(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_param_marks.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        """
import pytest

@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1, marks=pytest.mark.xfail(reason="known failure")),
        pytest.param(2, marks=pytest.mark.skip(reason="disabled")),
    ],
)
def test_marked_param(value):
    assert value > 0
""",
        encoding="utf-8",
    )

    assert not governance_score._regression_test_reference_exists(
        tmp_path,
        "tests/test_param_marks.py::test_marked_param[1]",
    )


def test_javascript_regression_test_syntax_error_is_actionable(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "tests" / "broken.test.ts"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        'test.only("broken case", () => {\n',
        encoding="utf-8",
    )
    _write_registry(
        tmp_path,
        [_closed_defect("tests/broken.test.ts::broken case")],
    )

    with pytest.raises(
        ValueError,
        match=(
            "known defect P1-test has invalid regression test .*"
            "cannot parse JavaScript regression test"
        ),
    ):
        governance_score._known_defect_checks(tmp_path)


def test_regression_test_path_escape_is_rejected(tmp_path: Path) -> None:
    _write_registry(
        tmp_path,
        [_closed_defect("../outside.py::test_outside")],
    )

    with pytest.raises(ValueError, match="escapes repository root"):
        governance_score._known_defect_checks(tmp_path)


def test_regression_test_unsupported_suffix_is_rejected(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "tests" / "test_registered.txt"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("test_registered\n", encoding="utf-8")
    _write_registry(
        tmp_path,
        [_closed_defect("tests/test_registered.txt::test_registered")],
    )

    with pytest.raises(ValueError, match="unsupported regression test suffix"):
        governance_score._known_defect_checks(tmp_path)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"id": 11}, "field 'id' must be a non-empty string"),
        ({"severity": ["P1"]}, "field 'severity' must be a non-empty string"),
        ({"status": False}, "field 'status' must be a non-empty string"),
        (
            {"regression_tests": "tests/test_registered.py::test_registered"},
            "field 'regression_tests' must be a non-empty list",
        ),
        (
            {"regression_tests": [7]},
            r"field regression_tests\[0\] must be a non-empty string",
        ),
        (
            {"fixed_commit": 7},
            "field 'fixed_commit' must be a 40-character",
        ),
        ({"paths": "tests/test_registered.py"}, "field 'paths' must be a list"),
    ],
)
def test_malformed_known_defect_field_types_raise_controlled_value_error(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    entry = _closed_defect(
        "tests/test_registered.py::test_registered",
        **overrides,
    )
    _write_registry(tmp_path, [entry])

    with pytest.raises(ValueError, match=message):
        governance_score._known_defect_checks(tmp_path)


def test_boolean_registry_version_is_not_coerced_to_integer(
    tmp_path: Path,
) -> None:
    _write_registry(tmp_path, [], version=True)

    with pytest.raises(ValueError, match="field 'version' must be integer 1"):
        governance_score._known_defect_checks(tmp_path)


def test_stale_evidence_is_rejected(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "version": 1,
                "commit": "f" * 40,
                "checks": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="stale or unsupported"):
        governance_score.build_report(
            root=ROOT,
            evidence_path=evidence,
            runner=_runner,
        )


def test_evidence_requires_command_and_zero_exit(tmp_path: Path) -> None:
    commit = governance_score._head_commit(ROOT)
    expected = governance_score._expected_evidence_commands(ROOT)
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "version": 1,
                "commit": commit,
                "checks": {
                    "full_tests": {
                        "command": expected["full_tests"],
                        "exit_code": 0,
                        "status": "passed",
                    },
                    "fault_matrix": {
                        "command": "true",
                        "exit_code": 0,
                        "status": "passed",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    checks = governance_score._evidence_checks(evidence, commit=commit)

    assert checks["full_tests"].passed is True
    assert checks["fault_matrix"].passed is False


def test_unknown_evidence_check_is_rejected(tmp_path: Path) -> None:
    commit = governance_score._head_commit(ROOT)
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "version": 1,
                "commit": commit,
                "checks": {
                    "migration_gate": {
                        "command": "true",
                        "exit_code": 0,
                        "status": "passed",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown governance evidence check"):
        governance_score._evidence_checks(evidence, commit=commit)


def test_evidence_cannot_override_a_failed_live_check(tmp_path: Path) -> None:
    commit = governance_score._head_commit(ROOT)
    evidence = tmp_path / "evidence.json"
    _write_evidence(evidence, commit=commit, check_names={"dead_code_zero"})

    def failing_dead_code_runner(
        command: list[str] | tuple[str, ...],
        _cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        if tuple(command) == governance_score.WORKTREE_STATUS_COMMAND:
            return subprocess.CompletedProcess(command, 0, "", "")
        if tuple(command) == governance_score.STATIC_COMMANDS["dead_code_zero"]:
            return subprocess.CompletedProcess(command, 17, "", "dead code")
        return subprocess.CompletedProcess(command, 0, "passed\n", "")

    report = governance_score.build_report(
        root=ROOT,
        evidence_path=evidence,
        runner=failing_dead_code_runner,
        generated_at="2026-07-31T00:00:00+00:00",
    )

    assert report["checks"]["dead_code_zero"]["passed"] is False
    assert report["checks"]["dead_code_zero"]["source"] == "command"
    assert report["checks"]["dead_code_zero"]["detail"] == "dead code"


def test_markdown_lists_failed_hard_gates() -> None:
    report = governance_score.build_report(
        root=ROOT,
        evidence_path=ROOT / ".audit_state/missing-governance-evidence.json",
        runner=_runner,
        generated_at="2026-07-29T00:00:00+00:00",
    )

    markdown = governance_score.render_markdown(report)

    assert "# Lumen Governance Score" in markdown
    assert "Failed Hard Gates" in markdown
    assert "`full_tests`" in markdown
    assert "`release_proof`" in markdown
