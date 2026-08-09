from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import Request
from sqlalchemy.exc import OperationalError

from app.routes import admin_release, admin_update_marker


class _FakeScalars:
    def __init__(self, values: list[str | None]) -> None:
        self._values = values

    def all(self) -> list[str | None]:
        return list(self._values)


class _FakeResult:
    def __init__(self, values: list[str | None]) -> None:
        self._values = values

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._values)


class _FakeDb:
    def __init__(
        self,
        values: list[str | None] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._values = values or []
        self._error = error

    async def execute(self, _statement: Any) -> _FakeResult:
        if self._error is not None:
            raise self._error
        return _FakeResult(self._values)


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/admin/release/rollback",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )


def _write_manifest(
    release_dir: Path,
    *,
    target_id: str,
    head: str = "0057_repair_concurrent_indexes",
) -> None:
    (release_dir / ".lumen_release.json").write_text(
        json.dumps({"id": target_id, "alembic_head_expected": head}),
        encoding="utf-8",
    )


def _configure_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    include_inventory: bool = True,
) -> tuple[str, Path, list[dict[str, object]]]:
    target_id = "20260806-010203"
    release_root = tmp_path / "lumen"
    release_dir = release_root / "releases" / target_id
    release_dir.mkdir(parents=True)
    releases = (
        [
            admin_release.ReleaseInfo(
                id=target_id,
                alembic_head_expected="0057_repair_concurrent_indexes",
            )
        ]
        if include_inventory
        else []
    )
    release_calls: list[dict[str, object]] = []

    class FakeLockService:
        def __init__(self, *, fallback_busy: Any) -> None:
            self.fallback_busy = fallback_busy

        async def acquire(self, **_kwargs: object) -> object:
            return object()

        async def release(self, _lock: object, **kwargs: object) -> None:
            release_calls.append(dict(kwargs))

    monkeypatch.setattr(admin_release, "SystemOperationLockService", FakeLockService)
    monkeypatch.setattr(admin_release, "update_lumen_root", lambda: release_root)
    monkeypatch.setattr(
        admin_release,
        "update_resolve_release",
        lambda _root, _target: release_dir,
    )
    monkeypatch.setattr(
        admin_release,
        "update_list_releases",
        lambda **_kwargs: releases,
    )
    monkeypatch.setattr(admin_release, "update_read_marker", lambda: None)
    monkeypatch.setattr(admin_release, "maintenance_marker_busy", lambda: False)
    monkeypatch.setattr(
        admin_release,
        "update_systemd_run_available",
        lambda: (_ for _ in ()).throw(AssertionError("runner must not be reached")),
    )
    return target_id, release_dir, release_calls


@pytest.mark.asyncio
async def test_rollback_rejects_missing_release_manifest_before_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target_id, _release_dir, release_calls = _configure_rollback(
        monkeypatch,
        tmp_path,
    )

    with pytest.raises(Exception) as exc_info:
        await admin_release.rollback_release(
            admin_release.RollbackIn(release_id=target_id),
            _request(),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            _FakeDb(["0057_repair_concurrent_indexes"]),  # type: ignore[arg-type]
        )

    assert getattr(exc_info.value, "status_code", None) == 409
    assert exc_info.value.detail["error"]["code"] == "release_manifest_unknown"
    assert release_calls == [{"succeeded": False, "reason": "release_manifest_unknown"}]


@pytest.mark.asyncio
async def test_rollback_rejects_multiple_or_mismatched_database_heads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target_id, release_dir, release_calls = _configure_rollback(
        monkeypatch,
        tmp_path,
    )
    _write_manifest(release_dir, target_id=target_id)

    with pytest.raises(Exception) as exc_info:
        await admin_release.rollback_release(
            admin_release.RollbackIn(release_id=target_id),
            _request(),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            _FakeDb(["0057_repair_concurrent_indexes", "other_head"]),  # type: ignore[arg-type]
        )

    assert getattr(exc_info.value, "status_code", None) == 409
    assert exc_info.value.detail["error"]["code"] == "schema_mismatch"
    assert release_calls == [{"succeeded": False, "reason": "schema_mismatch"}]


@pytest.mark.asyncio
async def test_rollback_db_probe_exception_never_launches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target_id, release_dir, release_calls = _configure_rollback(
        monkeypatch,
        tmp_path,
    )
    _write_manifest(release_dir, target_id=target_id)
    error = OperationalError("SELECT", {}, RuntimeError("db down"))

    with pytest.raises(Exception) as exc_info:
        await admin_release.rollback_release(
            admin_release.RollbackIn(release_id=target_id),
            _request(),
            SimpleNamespace(id="admin-1", email="admin@example.test"),
            _FakeDb(error=error),  # type: ignore[arg-type]
        )

    assert getattr(exc_info.value, "status_code", None) == 503
    assert exc_info.value.detail["error"]["code"] == "database_schema_probe_failed"
    assert release_calls == [
        {"succeeded": False, "reason": "database_schema_probe_failed"}
    ]


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _write_runtime_release(
    release_root: Path,
    shared_env: Path,
    release_id: str,
    *,
    tag: str,
    version: str,
    sha: str,
    build: bool = False,
) -> dict[str, tuple[str, str]]:
    release = release_root / "releases" / release_id
    release.mkdir(parents=True)
    (release / ".image-tag").write_text(f"{tag}\n", encoding="utf-8")
    (release / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    (release / ".lumen_release.json").write_text(
        json.dumps(
            {
                "id": release_id,
                "sha": sha,
                "branch": tag,
                "alembic_head_expected": "0057_repair_concurrent_indexes",
            }
        ),
        encoding="utf-8",
    )
    (release / "docker-compose.yml").write_text(
        "services:\n"
        "  api: {image: ignored}\n"
        "  worker: {image: ignored}\n"
        "  web: {image: ignored}\n"
        "  tgbot: {image: ignored}\n",
        encoding="utf-8",
    )
    (release / ".env").symlink_to(shared_env)

    identities: dict[str, tuple[str, str]] = {}
    records: dict[str, dict[str, object]] = {}
    compose_services: dict[str, str] = {}
    identity_seed = int(release_id[6:8]) * 16
    for index, service in enumerate(("api", "worker", "web", "tgbot"), start=1):
        identity_value = identity_seed + index
        image_id = f"sha256:{identity_value:064x}"
        digest = f"ghcr.io/cyeinfpro/lumen-{service}@sha256:{identity_value + 8:064x}"
        identities[service] = (image_id, digest)
        compose_services[service] = image_id
        records[service] = {
            "image_id": image_id,
            "repo_digests": [] if build else [digest],
            "revision": sha,
            "service": service,
            "source_ref": f"lumen-{service}:{tag}" if build else digest,
        }
    compose_services.update(
        {
            "api-green": identities["api"][0],
            "bootstrap": identities["api"][0],
            "migrate": identities["api"][0],
        }
    )
    (release / ".update-image-proof.json").write_text(
        json.dumps(
            {
                "build": build,
                "compose_services": compose_services,
                "schema": 1,
                "services": records,
                "source_commit": sha,
                "target_tag": tag,
            }
        ),
        encoding="utf-8",
    )
    override_lines = ["services:"]
    for service, image_id in sorted(compose_services.items()):
        override_lines.extend(
            (
                f"  {service}:",
                f'    image: "{image_id}"',
                "    pull_policy: never",
            )
        )
    (release / ".update-images.override.yml").write_text(
        "\n".join(override_lines) + "\n",
        encoding="utf-8",
    )
    return identities


def _rollback_script_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    target_build: bool = False,
) -> tuple[str, Path, str, str, Path]:
    release_root = tmp_path / "lumen"
    shared_env = release_root / "shared" / ".env"
    shared_env.parent.mkdir(parents=True)
    original_id = "20260801-010203"
    target_id = "20260802-010203"
    original_tag = "v1.2.103"
    target_tag = "v1.2.104"
    original_identities = _write_runtime_release(
        release_root,
        shared_env,
        original_id,
        tag=original_tag,
        version="1.2.103",
        sha="a" * 40,
    )
    _write_runtime_release(
        release_root,
        shared_env,
        target_id,
        tag=target_tag,
        version="1.2.104",
        sha="b" * 40,
        build=target_build,
    )
    shared_env.write_text(
        "\n".join(
            (
                f"LUMEN_IMAGE_TAG={original_tag}",
                "LUMEN_VERSION=1.2.103",
                f"LUMEN_API_IMAGE_REF={original_identities['api'][1]}",
                f"LUMEN_WORKER_IMAGE_REF={original_identities['worker'][1]}",
                f"LUMEN_WEB_IMAGE_REF={original_identities['web'][1]}",
                f"LUMEN_TGBOT_IMAGE_REF={original_identities['tgbot'][1]}",
                "TELEGRAM_BOT_TOKEN=test-token",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (release_root / "current").symlink_to(f"releases/{original_id}")
    (release_root / "previous").symlink_to(f"releases/{target_id}")
    marker = release_root / ".update-running"
    marker.write_text("running\n", encoding="utf-8")
    monkeypatch.setattr(admin_release, "update_update_marker_path", lambda: marker)
    script = admin_release._build_rollback_script(
        target_id=target_id,
        lumen_root=release_root,
    )
    return script, release_root, original_id, target_id, marker


def _install_rollback_command_stubs(
    tmp_path: Path,
    release_root: Path,
) -> tuple[Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$TEST_COMMAND_LOG"
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
printf 'curl %s\n' "$*" >> "$TEST_COMMAND_LOG"
[ "${TEST_HTTP_UNHEALTHY:-0}" != "1" ] || exit 22
exit 0
""",
    )

    image_rows: list[tuple[str, str, str]] = []
    for release_id in ("20260801-010203", "20260802-010203"):
        proof = json.loads(
            (
                release_root / "releases" / release_id / ".update-image-proof.json"
            ).read_text(encoding="utf-8")
        )
        for service, record in proof["services"].items():
            repo_digests = record["repo_digests"]
            image_rows.append(
                (
                    str(record["image_id"]),
                    str(repo_digests[0]) if repo_digests else "",
                    str(service),
                )
            )
    _write_executable(
        fake_bin / "docker",
        f"""#!/usr/bin/env bash
set -u
printf 'docker %s\n' "$*" >> "$TEST_COMMAND_LOG"
if [ "${{TEST_DOCKER_UNAVAILABLE:-0}}" = "1" ]; then
  exit 127
fi
command="${{1:-}}"
shift || true
if [ "$command" = "compose" ]; then
  if [ "${{1:-}}" = "version" ]; then
    exit 0
  fi
  if [ "${{1:-}}" = "--profile" ]; then
    shift 2
  fi
  action="${{1:-}}"
  shift || true
  case "$action" in
    up)
      current="$(basename "$(readlink "$TEST_ROLLBACK_ROOT/current")")"
      if [ "${{TEST_FAIL_TARGET:-0}}" = "1" ] \
          && [ "$current" = "$TEST_ROLLBACK_TARGET" ]; then
        exit 42
      fi
      exit 0
      ;;
    stop)
      exit 0
      ;;
    ps)
      service="${{@: -1}}"
      printf 'cid-%s\n' "$service"
      exit 0
      ;;
  esac
fi
if [ "$command" = "inspect" ]; then
  format="${{2:-}}"
  container="${{3:-}}"
  service="${{container#cid-}}"
  case "$format" in
    *State.Health*) printf 'healthy\n'; exit 0 ;;
    *'{{.Image}}'*)
      current="$(basename "$(readlink "$TEST_ROLLBACK_ROOT/current")")"
      python3 - "$TEST_ROLLBACK_ROOT" "$current" "$service" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
release_id = sys.argv[2]
service = sys.argv[3]
proof = json.loads(
    (root / "releases" / release_id / ".update-image-proof.json").read_text()
)
print(proof["compose_services"][service])
PY
      exit $?
      ;;
  esac
fi
if [ "$command" = "image" ] && [ "${{1:-}}" = "inspect" ]; then
  format="${{3:-}}"
  image_id="${{4:-}}"
  if [ "$format" = "{{{{.Id}}}}" ]; then
    printf '%s\n' "$image_id"
    exit 0
  fi
  case "$image_id" in
{os.linesep.join(f"    {image_id}) printf '%s\\n' '{digest}'; exit 0 ;;" for image_id, digest, _service in image_rows)}
  esac
fi
exit 1
""",
    )
    return fake_bin, command_log


def _run_rollback_script(
    script: str,
    *,
    fake_bin: Path,
    command_log: Path,
    release_root: Path,
    target_id: str,
    fail_target: bool = False,
    docker_unavailable: bool = False,
    http_unhealthy: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "TEST_COMMAND_LOG": str(command_log),
            "TEST_DOCKER_UNAVAILABLE": "1" if docker_unavailable else "0",
            "TEST_FAIL_TARGET": "1" if fail_target else "0",
            "TEST_HTTP_UNHEALTHY": "1" if http_unhealthy else "0",
            "TEST_ROLLBACK_ROOT": str(release_root),
            "TEST_ROLLBACK_TARGET": target_id,
            "LUMEN_ROLLBACK_VERIFY_ATTEMPTS": "1",
        }
    )
    return subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _read_authoritative_marker(
    marker_path: Path,
) -> admin_update_marker.UpdateMarker | None:
    return admin_update_marker.read_marker(
        marker_path,
        parse_marker=admin_update_marker.parse_marker_text,
        marker_is_live_fn=lambda marker: admin_update_marker.marker_is_live(
            marker,
            trigger_only_mode=lambda: False,
            marker_is_stale_fn=lambda _started_at: True,
            unit_is_running_fn=lambda _unit: False,
            pid_is_running_fn=lambda _pid: False,
        ),
    )


def _env_values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


def test_rollback_script_declares_fail_closed_state_and_runtime_proofs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script, _root, _original_id, _target_id, _marker = _rollback_script_fixture(
        monkeypatch,
        tmp_path,
    )

    for status in (
        "target_applied",
        "failed_recovered_original",
        "failed_original_unhealthy",
        "manual_required",
    ):
        assert status in script
    assert "rollback_status=rolled_back" not in script
    assert "requested_operation_status=failed" in script
    assert "runtime_recovery_status=original_healthy" in script
    assert "docker compose is unavailable for release" in script
    assert "! command -v docker" not in script
    for service in ("api", "worker", "web", "tgbot"):
        assert service in script
    assert "/readyz" in script
    assert "3000/healthz" in script
    assert ".update-image-proof.json" in script
    assert "verify_env_identity" in script
    assert "link_release_id" in script

    syntax = subprocess.run(
        ["bash", "-n"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_target_release_is_only_success_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script, release_root, _original_id, target_id, marker = _rollback_script_fixture(
        monkeypatch, tmp_path
    )
    fake_bin, command_log = _install_rollback_command_stubs(tmp_path, release_root)

    result = _run_rollback_script(
        script,
        fake_bin=fake_bin,
        command_log=command_log,
        release_root=release_root,
        target_id=target_id,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "status=target_applied rc=0" in result.stdout
    assert "key=requested_operation_status value=succeeded" in result.stdout
    assert "key=runtime_recovery_status value=not_required" in result.stdout
    assert os.readlink(release_root / "current") == f"releases/{target_id}"
    assert not marker.exists()


def test_build_release_sets_and_verifies_shared_image_refs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script, release_root, _original_id, target_id, marker = _rollback_script_fixture(
        monkeypatch, tmp_path, target_build=True
    )
    fake_bin, command_log = _install_rollback_command_stubs(tmp_path, release_root)

    result = _run_rollback_script(
        script,
        fake_bin=fake_bin,
        command_log=command_log,
        release_root=release_root,
        target_id=target_id,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    proof = json.loads(
        (release_root / "releases" / target_id / ".update-image-proof.json").read_text(
            encoding="utf-8"
        )
    )
    values = _env_values(release_root / "shared" / ".env")
    for service, key in (
        ("api", "LUMEN_API_IMAGE_REF"),
        ("worker", "LUMEN_WORKER_IMAGE_REF"),
        ("web", "LUMEN_WEB_IMAGE_REF"),
        ("tgbot", "LUMEN_TGBOT_IMAGE_REF"),
    ):
        assert values[key] == proof["compose_services"][service]
    assert not marker.exists()


def test_target_failure_recovers_original_but_request_stays_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script, release_root, original_id, target_id, marker = _rollback_script_fixture(
        monkeypatch, tmp_path
    )
    fake_bin, command_log = _install_rollback_command_stubs(tmp_path, release_root)

    result = _run_rollback_script(
        script,
        fake_bin=fake_bin,
        command_log=command_log,
        release_root=release_root,
        target_id=target_id,
        fail_target=True,
    )

    assert result.returncode == 1, result.stderr + result.stdout
    assert "status=failed_recovered_original rc=1" in result.stdout
    assert "key=requested_operation_status value=failed" in result.stdout
    assert "key=runtime_recovery_status value=original_healthy" in result.stdout
    assert os.readlink(release_root / "current") == f"releases/{original_id}"
    assert "LUMEN_IMAGE_TAG=v1.2.103" in (release_root / "shared" / ".env").read_text(
        encoding="utf-8"
    )
    assert not marker.exists()
    commands = command_log.read_text(encoding="utf-8")
    for service in ("api", "worker", "web", "tgbot"):
        assert f"ps --status running --quiet {service}" in commands
    assert "curl --noproxy * -fsS --max-time 5 -o /dev/null" in commands


def test_compose_unavailable_during_recovery_requires_manual_action(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script, release_root, original_id, target_id, marker = _rollback_script_fixture(
        monkeypatch, tmp_path
    )
    fake_bin, command_log = _install_rollback_command_stubs(tmp_path, release_root)

    result = _run_rollback_script(
        script,
        fake_bin=fake_bin,
        command_log=command_log,
        release_root=release_root,
        target_id=target_id,
        docker_unavailable=True,
    )

    assert result.returncode == 1, result.stderr + result.stdout
    assert "status=manual_required rc=1" in result.stdout
    assert "failed_recovered_original" not in result.stdout
    assert os.readlink(release_root / "current") == f"releases/{original_id}"
    assert marker.exists()
    authoritative = _read_authoritative_marker(marker)
    assert authoritative is not None
    assert authoritative.pid == 0
    assert authoritative.owner == "manual"
    assert authoritative.state == "manual_required"


def test_recovered_original_with_failed_verification_stays_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script, release_root, original_id, target_id, marker = _rollback_script_fixture(
        monkeypatch, tmp_path
    )
    fake_bin, command_log = _install_rollback_command_stubs(tmp_path, release_root)

    result = _run_rollback_script(
        script,
        fake_bin=fake_bin,
        command_log=command_log,
        release_root=release_root,
        target_id=target_id,
        http_unhealthy=True,
    )

    assert result.returncode == 1, result.stderr + result.stdout
    assert "status=failed_original_unhealthy rc=1" in result.stdout
    assert "key=runtime_recovery_status value=original_unhealthy" in result.stdout
    assert os.readlink(release_root / "current") == f"releases/{original_id}"
    assert marker.exists()
    authoritative = _read_authoritative_marker(marker)
    assert authoritative is not None
    assert authoritative.pid == 0
    assert authoritative.owner == "manual"
    assert authoritative.state == "failed_original_unhealthy"


@pytest.mark.asyncio
async def test_detached_cleanup_preserves_authoritative_manual_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    script, release_root, _original_id, target_id, marker = _rollback_script_fixture(
        monkeypatch, tmp_path
    )
    fake_bin, command_log = _install_rollback_command_stubs(tmp_path, release_root)
    result = _run_rollback_script(
        script,
        fake_bin=fake_bin,
        command_log=command_log,
        release_root=release_root,
        target_id=target_id,
        docker_unavailable=True,
    )
    assert result.returncode == 1

    class _FinishedProcess:
        pid = 999_999

        def wait(self) -> int:
            return result.returncode

    monkeypatch.setattr(
        admin_release,
        "update_read_marker",
        lambda: _read_authoritative_marker(marker),
    )
    await admin_release._cleanup_marker_when_done(_FinishedProcess())  # type: ignore[arg-type]

    authoritative = _read_authoritative_marker(marker)
    assert authoritative is not None
    assert authoritative.state == "manual_required"
