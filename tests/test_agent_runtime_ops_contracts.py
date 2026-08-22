from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.yml"
WORKFLOW = ROOT / ".github/workflows/docker-release.yml"
REQUIRED_AGENT_ENV = {
    "AGENT_ENABLED",
    "UI_NAV_AGENT_VISIBLE",
    "AGENT_MAX_TURNS",
    "AGENT_MAX_TOOL_CALLS",
    "AGENT_MAX_IMAGE_TOOL_CALLS",
    "AGENT_MAX_IMAGES_PER_RUN",
    "AGENT_MAX_REFERENCE_IMAGES",
    "AGENT_MAX_OUTPUT_TOKENS",
    "AGENT_RUN_TIMEOUT_SECONDS",
    "AGENT_TOOL_TIMEOUT_SECONDS",
    "AGENT_CAPABILITY_TTL_SECONDS",
    "AGENT_RUNTIME_URL",
    "AGENT_RUNTIME_SHARED_SECRET",
    "AGENT_TOOL_CAPABILITY_SECRET",
    "AGENT_TOOL_GATEWAY_URL",
    "AGENT_RUNTIME_HEALTH_TIMEOUT_SECONDS",
    "AGENT_RUNTIME_MAX_CONCURRENT_RUNS",
    "AGENT_RUNTIME_REQUEST_BODY_TIMEOUT_SECONDS",
    "LUMEN_AGENT_RUNTIME_IMAGE_REF",
}


def _dotenv_keys(path: Path) -> set[str]:
    return {
        line.partition("=")[0]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }


def _load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_agent_environment_surface_is_complete_and_closed_by_default() -> None:
    env_path = ROOT / ".env.example"
    keys = _dotenv_keys(env_path)
    values = {
        line.partition("=")[0]: line.partition("=")[2]
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    assert REQUIRED_AGENT_ENV <= keys
    assert values["AGENT_ENABLED"] == "0"
    assert values["UI_NAV_AGENT_VISIBLE"] == "0"
    assert values["AGENT_RUNTIME_URL"] == "http://agent-runtime:8090"
    assert values["AGENT_RUNTIME_SHARED_SECRET"] == ""
    assert values["AGENT_TOOL_CAPABILITY_SECRET"] == ""


def test_agent_runtime_compose_is_private_bounded_and_read_only() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    runtime = compose["services"]["agent-runtime"]

    assert runtime["profiles"] == ["agent-runtime"]
    assert "LUMEN_API_IMAGE_REF" in runtime["image"]
    assert runtime["networks"] == ["lumen_backend"]
    assert "ports" not in runtime
    assert "volumes" not in runtime
    assert runtime["read_only"] is True
    assert runtime["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in runtime["security_opt"]
    assert runtime["user"] == (
        "${LUMEN_AGENT_RUNTIME_UID:-1000}:"
        "${LUMEN_AGENT_RUNTIME_GID:-1000}"
    )
    assert runtime["stop_signal"] == "SIGTERM"
    assert runtime["stop_grace_period"] == "30s"
    assert runtime["cpus"]
    assert runtime["mem_limit"]
    assert len(runtime["tmpfs"]) == 1
    assert "noexec" in runtime["tmpfs"][0]
    assert "size=${AGENT_RUNTIME_TMPFS_SIZE:-64m}" in runtime["tmpfs"][0]
    assert runtime["healthcheck"]["test"][0:2] == ["CMD", "node"]


def test_all_active_compose_variants_account_for_agent_runtime() -> None:
    development = yaml.safe_load(
        (ROOT / "docker-compose.dev.yml").read_text(encoding="utf-8")
    )
    local = yaml.safe_load(
        (ROOT / "deploy/docker/docker-compose.local.yml").read_text(
            encoding="utf-8"
        )
    )
    public_dns = yaml.safe_load(
        (ROOT / "docker-compose.public-dns.yml").read_text(encoding="utf-8")
    )
    blue_green = yaml.safe_load(
        (ROOT / "docker-compose.bluegreen.yml").read_text(encoding="utf-8")
    )

    assert development["services"]["agent-runtime"]["build"]["context"] == (
        "./apps/agent-runtime"
    )
    assert local["services"]["agent-runtime"]["container_name"] == (
        "lumen-local-agent-runtime"
    )
    assert public_dns["services"]["agent-runtime"]["dns"]
    assert blue_green["services"]["agent-runtime"]["profiles"] == [
        "agent-runtime"
    ]
    assert "api-green" in blue_green["services"]
    assert blue_green["services"]["api-green"]["networks"] == ["lumen_backend"]


def test_release_manifest_keeps_legacy_map_and_adds_runtime_component() -> None:
    module = _load_module("agent_ops_release_manifest", "scripts/build_release_manifest.py")
    digests = {
        service: f"sha256:{index:064x}"
        for index, service in enumerate(module.SERVICES, start=1)
    }
    manifest = module.build_release_manifest(
        version="v1.2.134",
        commit_sha="a" * 40,
        short_sha="a" * 7,
        registry="ghcr.io/cyeinfpro",
        alembic_heads=["0069_agent_foundation"],
        image_digests=digests,
        dependency_images={
            "python": "python:3.12-slim@sha256:" + "b" * 64,
            "postgres": "pgvector/pgvector:pg16@sha256:" + "c" * 64,
            "redis": "redis:7.4-alpine@sha256:" + "d" * 64,
        },
        component_dependency_images={
            "node": "node:22.22.0-alpine@sha256:" + "e" * 64,
        },
        generated_at="2026-08-22T00:00:00Z",
    )

    assert set(manifest["images"]) == {"api", "worker", "tgbot", "web"}
    assert set(manifest["components"]) == {"agent-runtime"}
    assert manifest["components"]["agent-runtime"]["artifacts"] == {
        "signature": "sigstore-keyless",
        "sbom": "spdx-json",
        "sbom_attestation_type": "spdxjson",
    }
    assert manifest["component_dependencies"]["node"]["immutable_ref"].startswith(
        "node:22.22.0-alpine@sha256:"
    )


def test_release_workflow_builds_signs_attests_and_promotes_five_images() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    parsed = yaml.load(workflow, Loader=yaml.BaseLoader)
    matrix = parsed["jobs"]["build"]["strategy"]["matrix"]["image"]
    names = {item["name"] for item in matrix}

    assert names == {
        "lumen-api",
        "lumen-worker",
        "lumen-tgbot",
        "lumen-agent-runtime",
    }
    assert "merge-web" in parsed["jobs"]
    assert "expected five signed service digests" in workflow
    assert 'services=(api worker agent-runtime tgbot web)' in workflow
    assert 'cosign attest --yes --type spdxjson' in workflow
    assert 'cosign verify-attestation' in workflow
    assert '--image-digest "agent-runtime=${AGENT_RUNTIME_DIGEST}"' in workflow
    assert '/tmp/release-sboms/*.spdx.json' in workflow
    assert "! touch /app/.lumen-write-probe" in workflow


def test_installer_update_and_rollback_manage_agent_runtime_explicitly() -> None:
    sources = {
        "install": (ROOT / "scripts/install/services.sh").read_text(encoding="utf-8"),
        "update": (ROOT / "scripts/update/services/restart.sh").read_text(
            encoding="utf-8"
        ),
        "health": (ROOT / "scripts/update/services/health.sh").read_text(
            encoding="utf-8"
        ),
        "cli": (ROOT / "scripts/lumenctl/compose.sh").read_text(encoding="utf-8"),
        "rollback": (
            ROOT / "apps/api/app/routes/admin_release_rollback_script.py"
        ).read_text(encoding="utf-8"),
    }

    assert "agent-runtime api worker web" in sources["install"]
    assert "_target_services=(agent-runtime api worker web)" in sources["update"]
    assert "HEALTH_SERVICES=(agent-runtime api worker web)" in sources["health"]
    assert "agent-runtime api worker web" in sources["cli"]
    assert 'required.append("agent-runtime")' in sources["rollback"]
    assert "LUMEN_AGENT_RUNTIME_IMAGE_REF" in sources["rollback"]
    check = (ROOT / "scripts/update/release/check.sh").read_text(encoding="utf-8")
    assert 'docker inspect lumen-agent-runtime' in check
    assert 'agent_runtime "missing_redeploy_required"' in check


def test_python_release_source_contains_runtime_local_build_payload() -> None:
    dockerfile = (ROOT / "Dockerfile.python").read_text(encoding="utf-8")
    source_helpers = (ROOT / "scripts/update/release/source_helpers.sh").read_text(
        encoding="utf-8"
    )

    assert "COPY apps/agent-runtime/ apps/agent-runtime/" in dockerfile
    assert "docker-compose.dev.yml" in dockerfile
    assert "docker-compose.bluegreen.yml" in source_helpers
    assert "docker-compose.public-dns.yml" in source_helpers
    assert "local optional_paths=(" in source_helpers
    assert "apps" in source_helpers


def test_agent_runtime_package_participates_in_product_version_sync() -> None:
    product_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    package = json.loads(
        (ROOT / "apps/agent-runtime/package.json").read_text(encoding="utf-8")
    )
    lock = json.loads(
        (ROOT / "apps/agent-runtime/package-lock.json").read_text(encoding="utf-8")
    )

    assert package["version"] == product_version
    assert lock["version"] == product_version
    assert lock["packages"][""]["version"] == product_version


def test_live_harnesses_prove_distinct_tool_preflight_and_isolation_contracts() -> None:
    provider_harness = (
        ROOT / "apps/agent-runtime/scripts/live-provider-check.ts"
    ).read_text(encoding="utf-8")
    full_stack = (ROOT / "apps/web/e2e/agent-live.spec.ts").read_text(
        encoding="utf-8"
    )

    assert "const systemPrompt = tool" in provider_harness
    assert "call only explicitly registered tools" in provider_harness
    assert "do not call tools" in provider_harness
    for scenario in (
        'scenario("insufficient_balance"',
        'scenario("byok_unavailable"',
        'scenario("vision_unavailable"',
    ):
        assert scenario in full_stack
    assert "crossUserStatuses" in full_stack
    assert "/api/agent/runs/" in full_stack
    assert "/api/images/" in full_stack
    assert "api[_-]?key|authorization|capability|tool[_-]?token" in full_stack
