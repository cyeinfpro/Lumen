from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_backend_ci_uses_real_redis_without_repeating_frontend() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    test_script = (ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")

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
