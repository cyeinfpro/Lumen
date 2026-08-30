from __future__ import annotations

import shlex
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _docker_copy_sources(source: str) -> list[str]:
    copied: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line.startswith("COPY "):
            continue
        tokens = [
            token for token in shlex.split(line)[1:] if not token.startswith("--")
        ]
        copied.extend(token.rstrip("/") for token in tokens[:-1])
    return copied


def test_backend_ci_uses_real_redis_without_repeating_frontend() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    test_script = (ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")

    assert "image: pgvector/pgvector:pg16" in workflow
    assert "image: postgres:16" not in workflow
    assert "redis:" in workflow
    assert "image: redis:7.4-alpine" in workflow
    assert "LUMEN_TEST_REDIS_URL: redis://127.0.0.1:6379/15" in workflow
    assert (
        "run: uv run ruff check packages/core apps/api apps/worker "
        "apps/tgbot image-job tests"
    ) in workflow
    assert 'LUMEN_TEST_SKIP_GOVERNANCE: "1"' in workflow
    assert 'LUMEN_TEST_SKIP_WEB: "1"' in workflow
    assert "node-version: '22'" in workflow
    assert 'if [ "${LUMEN_TEST_SKIP_GOVERNANCE:-0}" != "1" ]; then' in test_script
    assert 'if [ "${LUMEN_TEST_SKIP_WEB:-0}" != "1" ]; then' in test_script


def test_cross_language_contract_tooling_precedes_backend_tests() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "docker-release.yml").read_text(
        encoding="utf-8"
    )
    test_script = (ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")

    ci_dependencies = workflow.index("Install Agent Runtime contract dependencies")
    assert ci_dependencies < workflow.index("Run impacted backend tests")
    assert ci_dependencies < workflow.index("Run full backend tests")
    assert release.index("Agent Runtime dependencies") < release.index("Python tests")
    assert 'node_modules/.bin/tsx"' in test_script
    assert test_script.index("ensure_agent_runtime_deps\n") < test_script.index(
        'echo "==> apps/worker/tests"'
    )


def test_python_dockerfile_stages_every_uv_workspace_member() -> None:
    workspace = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "tool"
    ]["uv"]["workspace"]["members"]
    dockerfile = (ROOT / "Dockerfile.python").read_text(encoding="utf-8")
    before_sync, after_sync = dockerfile.split(
        "RUN uv sync --frozen --no-dev --all-packages",
        maxsplit=1,
    )
    source_copies = _docker_copy_sources(after_sync)
    ignored_roots = {
        line.strip().rstrip("/")
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip()
        and not line.lstrip().startswith(("#", "!"))
        and not any(character in line for character in "*?[")
    }

    for member in workspace:
        metadata_copy = f"COPY {member}/pyproject.toml {member}/pyproject.toml"
        assert metadata_copy in before_sync, (
            f"Dockerfile.python must stage {member}/pyproject.toml before uv sync"
        )
        assert member not in ignored_roots, (
            f".dockerignore hides uv workspace member {member}"
        )
        assert any(
            member == source or member.startswith(f"{source}/")
            for source in source_copies
        ), f"Dockerfile.python must copy {member} source after uv sync"

    assert "COPY image-job/README.md image-job/README.md" in before_sync
    assert "image-job/image_job/__init__.py" in before_sync
