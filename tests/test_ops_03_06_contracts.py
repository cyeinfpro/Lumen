from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "update" / "bootstrap.sh"
LIB = ROOT / "scripts" / "lib.sh"
CHECKER = ROOT / "scripts" / "check_immutable_images.py"
BACKUP_SERVICE = ROOT / "deploy" / "systemd" / "lumen-backup.service"


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "value",
    ("standrad", "FAST ", "", "main", "1", "../../fast"),
)
def test_invalid_update_mode_fails_before_side_effects(
    tmp_path: Path,
    value: str,
) -> None:
    deploy_root = tmp_path / "deploy"
    shared = deploy_root / "shared"
    shared.mkdir(parents=True)
    result = _run_bash(
        f"""
        set -uo pipefail
        SCRIPT_DIR={shlex.quote(str(ROOT / "scripts"))}
        LUMEN_DEPLOY_ROOT={shlex.quote(str(deploy_root))}
        _LUMEN_UPDATE_INPUT_DEPLOY_ROOT="$LUMEN_DEPLOY_ROOT"
        _LUMEN_UPDATE_INPUT_UPDATE_ROOT=""
        _LUMEN_UPDATE_INPUT_DATA_ROOT=""
        _LUMEN_UPDATE_INPUT_DB_ROOT=""
        _LUMEN_UPDATE_INPUT_BACKUP_ROOT=""
        _LUMEN_UPDATE_INPUT_POSTGRES_UID=""
        _LUMEN_UPDATE_INPUT_POSTGRES_GID=""
        _LUMEN_UPDATE_INPUT_REDIS_UID=""
        _LUMEN_UPDATE_INPUT_REDIS_GID=""
        _LUMEN_UPDATE_INPUT_APP_UID=""
        _LUMEN_UPDATE_INPUT_APP_GID=""
        _LUMEN_UPDATE_INPUT_APP_STORAGE_GID=""
        LUMEN_UPDATE_MODE={shlex.quote(value)}
        lumen_resolve_repo_root() {{ printf '%s\\n' {shlex.quote(str(ROOT))}; }}
        lumen_resolve_deploy_root() {{ printf '%s\\n' "$LUMEN_DEPLOY_ROOT"; }}
        lumen_release_shared_env_path_safe() {{ return 0; }}
        lumen_require_systemd_flock() {{ return 0; }}
        lumen_env_value() {{ return 1; }}
        log_error() {{ printf 'error:%s\\n' "$*" >&2; }}
        log_warn() {{ printf 'warn:%s\\n' "$*" >&2; }}
        log_info() {{ :; }}
        emit_info() {{ :; }}
        lumen_install_signal_handlers() {{
            printf 'signal-handlers\\n' >> {shlex.quote(str(tmp_path / "effects"))}
        }}
        . {shlex.quote(str(BOOTSTRAP))}
        """
    )

    assert result.returncode == 64, result.stderr + result.stdout
    assert not (tmp_path / "effects").exists()


def test_unset_update_mode_keeps_documented_fast_default(tmp_path: Path) -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert '${LUMEN_UPDATE_MODE+x}' in source
    assert 'raw_update_mode="fast"' in source
    assert "exit 64" in source


@pytest.mark.parametrize("channel", ("pinned", "minor", "major"))
@pytest.mark.parametrize(
    "current",
    (None, "", "main", "latest", "garbage", "v1;touch-x", "../../v1.2.3"),
)
def test_conservative_channels_never_fall_back_to_main(
    tmp_path: Path,
    channel: str,
    current: str | None,
) -> None:
    env_file = tmp_path / ".env"
    if current is not None:
        env_file.write_text(f"LUMEN_IMAGE_TAG={current}\n", encoding="utf-8")
    result = _run_bash(
        f"""
        set -uo pipefail
        . {shlex.quote(str(LIB))}
        lumen_image_tag_resolve {shlex.quote(channel)} {shlex.quote(str(env_file))}
        """
    )

    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_invalid_resolved_tag_and_literal_channel_are_rejected(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LUMEN_IMAGE_TAG=v1.2.3\n", encoding="utf-8")
    override = _run_bash(
        f"""
        set -uo pipefail
        . {shlex.quote(str(LIB))}
        LUMEN_UPDATE_RESOLVED_TAG=../../main
        lumen_image_tag_resolve main {shlex.quote(str(env_file))}
        """
    )
    literal = _run_bash(
        f"""
        set -uo pipefail
        . {shlex.quote(str(LIB))}
        lumen_image_tag_resolve 'v1;touch-x' {shlex.quote(str(env_file))}
        """
    )

    assert override.returncode == 64
    assert override.stdout.strip() == ""
    assert literal.returncode == 64
    assert literal.stdout.strip() == ""


def test_backup_service_retries_lock_deferrals_without_start_limit() -> None:
    service = BACKUP_SERVICE.read_text(encoding="utf-8")

    assert "StartLimitIntervalSec=0" in service
    assert "StartLimitBurst=" not in service
    assert "Restart=on-failure" in service
    assert "RestartSec=60s" in service


@pytest.mark.parametrize(
    ("payload", "expected_rc"),
    (
        ("", 2),
        ("ghcr.io/cyeinfpro/lumen-api:latest\n", 1),
        ("ghcr.io/cyeinfpro/lumen-api:main\n", 1),
        ("ghcr.io/cyeinfpro/lumen-api:sha-deadbee\n", 1),
        ("ghcr.io/cyeinfpro/lumen-api@sha256:abc\n", 1),
        (f"ghcr.io/cyeinfpro/lumen-api@sha256:{'A' * 64}\n", 1),
        (f" ghcr.io/cyeinfpro/lumen-api@sha256:{'a' * 64}\n", 1),
        (f"ghcr.io/cyeinfpro/lumen-api@sha256:{'a' * 64} \n", 1),
        (" \n", 1),
        (f"ghcr.io/cyeinfpro/lumen-api@sha256:{'a' * 64}\n", 0),
    ),
)
def test_immutable_image_checker_contract(payload: str, expected_rc: int) -> None:
    result = subprocess.run(
        [os.fspath(CHECKER)],
        cwd=ROOT,
        text=True,
        input=payload,
        capture_output=True,
        check=False,
    )

    assert result.returncode == expected_rc, result.stderr + result.stdout


def test_production_compose_requires_complete_digest_references() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    bluegreen = (ROOT / "docker-compose.bluegreen.yml").read_text(encoding="utf-8")
    expected = {
        "postgres": "LUMEN_POSTGRES_IMAGE_REF",
        "redis": "LUMEN_REDIS_IMAGE_REF",
        "api": "LUMEN_API_IMAGE_REF",
        "worker": "LUMEN_WORKER_IMAGE_REF",
        "web": "LUMEN_WEB_IMAGE_REF",
        "tgbot": "LUMEN_TGBOT_IMAGE_REF",
        "migrate": "LUMEN_API_IMAGE_REF",
        "bootstrap": "LUMEN_API_IMAGE_REF",
    }

    for service, variable in expected.items():
        assert (
            f"image: ${{{variable}:?Set {variable} to name@sha256 digest}}"
            in compose
        ), service
    assert "build:" not in compose
    assert (
        "image: ${LUMEN_API_IMAGE_REF:?Set LUMEN_API_IMAGE_REF to name@sha256 digest}"
        in bluegreen
    )
    assert "build:" not in bluegreen
    assert "latest" not in bluegreen


def test_python_base_has_no_mutable_default() -> None:
    dockerfile = (ROOT / "Dockerfile.python").read_text(encoding="utf-8")

    assert "ARG PYTHON_BASE\n" in dockerfile
    assert "ARG PYTHON_BASE=" not in dockerfile
