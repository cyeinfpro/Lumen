"""Splitting release steps must retain every existing frontend quality gate."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/docker-release.yml"


def _step(name: str) -> str:
    source = WORKFLOW.read_text(encoding="utf-8")
    pattern = rf"^      - name: {re.escape(name)}\n(.*?)(?=^      - |^  [a-z]|\Z)"
    matches = re.findall(pattern, source, re.MULTILINE | re.DOTALL)
    assert len(matches) == 1, f"expected one mandatory {name} step"
    assert "        if:" not in matches[0]
    return matches[0]


def test_release_python_step_does_not_repeat_dedicated_web_or_runtime_gates():
    step = _step("Python tests")
    assert 'LUMEN_TEST_SKIP_WEB: "1"' in step
    assert 'LUMEN_TEST_SKIP_AGENT_RUNTIME: "1"' in step
    assert "run: bash scripts/test.sh" in step
    script = (ROOT / "scripts/test.sh").read_text(encoding="utf-8")
    assert 'uv run pytest tests "$@" --durations=15' in script


def test_full_web_tests_and_lint_remain_mandatory_after_one_dependency_install():
    dependencies = _step("Web dependencies")
    tests = _step("Web tests and lint")
    assert "working-directory: apps/web" in dependencies
    assert "run: npm ci" in dependencies
    assert "working-directory: apps/web" in tests
    assert "run: npm test && npm run lint" in tests
    source = WORKFLOW.read_text(encoding="utf-8")
    assert source.index("- name: Web dependencies") < source.index("- name: Web tests and lint")


def test_type_build_and_node_compatibility_checks_remain_independent_release_gates():
    assert "run: npm run type-check" in _step("Web type-check")
    assert "run: npm run build" in _step("Web build")
    assert "semanticIdempotency.test.ts" in _step("Verify semantic idempotency on Node 24")
    assert "npm test && npm run type-check && npm run lint && npm run build" in _step(
        "Agent Runtime tests and static gates"
    )
