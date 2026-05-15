from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from app.routes import admin_update
from app.services import update_check
from app.services.update_check import GitHubReleasesClient, UpdateCheckService


def test_update_paths_resolve_from_lumen_scripts_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "backup.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts_dir / "restore.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts_dir / "update.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(admin_update.settings, "backup_root", str(backup_root))
    monkeypatch.setattr(admin_update.settings, "lumen_scripts_dir", str(scripts_dir))

    assert admin_update._update_script() == scripts_dir / "update.sh"
    assert admin_update._update_log_path() == backup_root / ".update.log"
    assert admin_update._update_marker_path() == backup_root / ".update.running"


def test_proxy_env_is_replaced_for_update_process() -> None:
    env = {
        "HTTP_PROXY": "http://old",
        "https_proxy": "http://old-lower",
        "KEEP": "1",
    }

    admin_update._clean_proxy_env(env)
    admin_update._apply_proxy_env(env, "socks5h://127.0.0.1:1080")

    assert env["KEEP"] == "1"
    for key in (
        "LUMEN_UPDATE_PROXY_URL",
        "LUMEN_HTTP_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        assert env[key] == "socks5h://127.0.0.1:1080"


def test_update_proxy_can_be_loaded_from_shared_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shared_env = tmp_path / "shared" / ".env"
    shared_env.parent.mkdir()
    shared_env.write_text(
        "LUMEN_HTTP_PROXY='http://127.0.0.1:7890'\n"
        "NO_PROXY=localhost,127.0.0.1,::1,10.0.0.0/8\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LUMEN_SHARED_ENV", str(shared_env))

    env: dict[str, str] = {}
    proxy_url = admin_update._apply_dotenv_proxy_env(
        env,
        admin_update._shared_env_path(tmp_path / "current" / "scripts" / "update.sh"),
    )

    assert proxy_url == "http://127.0.0.1:7890"
    assert env["LUMEN_UPDATE_PROXY_URL"] == "http://127.0.0.1:7890"
    assert env["LUMEN_HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1,10.0.0.0/8"


def test_update_check_version_falls_back_to_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "missing-deploy-root"
    monkeypatch.setenv("LUMEN_VERSION", "1.2.4")
    monkeypatch.setenv("LUMEN_IMAGE_TAG", "v1.2.4")
    monkeypatch.setenv("LUMEN_UPDATE_VIA_TRIGGER", "1")
    monkeypatch.delenv("LUMEN_SHARED_ENV", raising=False)

    assert update_check._current_version(root) == "1.2.4"
    assert update_check._current_image_tag(root) == "v1.2.4"
    assert update_check._build_type(root) == "docker"


def test_update_runner_env_forces_target_tag_and_version() -> None:
    env = {
        "LUMEN_UPDATE_RESOLVED_TAG": "v1.2.4",
        "LUMEN_IMAGE_TAG": "v1.2.4",
        "LUMEN_VERSION": "1.2.4",
    }

    lines = admin_update._runner_env_lines(env)

    assert "LUMEN_UPDATE_RESOLVED_TAG=v1.2.4" in lines
    assert "LUMEN_IMAGE_TAG=v1.2.4" in lines
    assert "LUMEN_VERSION=1.2.4" in lines
    assert admin_update._version_from_update_tag("v1.2.4") == "1.2.4"
    assert admin_update._version_from_update_tag("main") is None


def test_update_step_fail_lines_are_reported_as_failed_done_steps() -> None:
    log = "\n".join(
        [
            "=== update trigger at=2026-05-03T00:00:00+00:00 user=1 proxy=none ===",
            "::lumen-step:: phase=fetch_release status=start ts=2026-05-03T00:00:01Z",
            "::lumen-info:: phase=fetch_release key=reason value=rsync_failed",
            "::lumen-step:: phase=fetch_release status=fail rc=1 ts=2026-05-03T00:00:02Z",
        ]
    )

    phases = admin_update._parse_steps(log)
    event, payload = admin_update._classify_log_line(
        "::lumen-step:: phase=fetch_release status=fail rc=1 ts=2026-05-03T00:00:02Z"
    )

    assert len(phases) == 1
    assert phases[0].phase == "fetch_release"
    assert phases[0].status == "done"
    assert phases[0].rc == 1
    assert phases[0].info["reason"] == "rsync_failed"
    assert event == "step"
    assert payload["status"] == "done"
    assert payload["rc"] == 1


def test_update_trigger_note_mentions_restart_and_health_check() -> None:
    # The response currently passes the note inline from trigger_update; keep the
    # user-facing contract visible even if that implementation is later refactored.
    source = Path(admin_update.__file__).read_text(encoding="utf-8")
    assert "重启运行进程并执行健康检查" in source


def test_systemd_run_command_uses_separate_unit_and_marker_cleanup(
    tmp_path: Path,
) -> None:
    command = admin_update._systemd_run_command(
        unit="lumen-update-test.service",
        root=tmp_path / "lumen",
        script=tmp_path / "lumen" / "scripts" / "update.sh",
        log_path=tmp_path / ".update.log",
        env_file=tmp_path / ".update.env",
        marker_path=tmp_path / ".update.running",
    )

    assert command[:4] == [
        "systemd-run",
        "--unit",
        "lumen-update-test.service",
        "--collect",
    ]
    assert f"WorkingDirectory={tmp_path / 'lumen'}" in command
    assert any(part.startswith("User=") for part in command)
    assert any(part.startswith("Group=") for part in command)
    joined = "\n".join(command)
    assert "trap cleanup EXIT" in joined
    assert '/usr/bin/env bash "$script"' in joined


def test_start_update_systemd_unit_writes_marker_and_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / "lumen" / "scripts"
    scripts_dir.mkdir(parents=True)
    script = scripts_dir / "update.sh"
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(admin_update.settings, "backup_root", str(backup_root))
    monkeypatch.setattr(
        admin_update,
        "_systemd_unit_name",
        lambda _started_at: "lumen-update-test.service",
    )

    captured: dict[str, object] = {}

    def fake_run(command, env, cwd):
        captured["command"] = command
        captured["env"] = env
        captured["cwd"] = cwd

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr(admin_update, "_run_systemd_command", fake_run)
    log_path = backup_root / ".update.log"
    log_path.parent.mkdir(parents=True)
    with log_path.open("a", encoding="utf-8") as log_fh:
        pid, unit = admin_update._start_update_systemd_unit(
            script=script,
            env={"PATH": "/bin", "LUMEN_UPDATE_NONINTERACTIVE": "1"},
            log_fh=log_fh,
            started_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )

    marker = (backup_root / ".update.running").read_text(encoding="utf-8")
    assert pid == 0
    assert unit == "lumen-update-test.service"
    assert "unit=lumen-update-test.service" in marker
    assert captured["cwd"] == tmp_path / "lumen"
    env_file = backup_root / ".update.lumen-update-test.service.env"
    env_text = env_file.read_text(encoding="utf-8")
    assert "export LUMEN_UPDATE_NONINTERACTIVE=1" in env_text
    assert "export LUMEN_UPDATE_SYSTEMD_UNIT=lumen-update-test.service" in env_text


def test_start_update_via_path_unit_writes_trigger_and_waits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(admin_update.settings, "backup_root", str(backup_root))

    # Pretend the runner unit goes active immediately.
    monkeypatch.setattr(admin_update, "_unit_is_running", lambda unit: True)

    log_path = backup_root / ".update.log"
    log_path.parent.mkdir(parents=True)
    with log_path.open("a", encoding="utf-8") as log_fh:
        outcome = admin_update._start_update_via_path_unit(
            env={
                "LUMEN_UPDATE_NONINTERACTIVE": "1",
                "LUMEN_UPDATE_GIT_PULL": "1",
                "LUMEN_UPDATE_BUILD": "0",
                "LUMEN_UPDATE_CHANNEL": "pinned",
                "LUMEN_UPDATE_FORCE_REDEPLOY": "1",
                "LUMEN_REPO_DIR": "/root/Lumen",
                "LUMEN_IMAGE_TAG": "v1.2.3",
                "HTTP_PROXY": "http://proxy.example:3128",
                "PATH": "/should/not/leak",
            },
            log_fh=log_fh,
            started_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )

    assert outcome == (0, admin_update._UPDATE_RUNNER_UNIT)

    marker = (backup_root / ".update.running").read_text(encoding="utf-8")
    assert f"unit={admin_update._UPDATE_RUNNER_UNIT}" in marker

    env_text = (backup_root / ".update.env").read_text(encoding="utf-8")
    assert "LUMEN_UPDATE_NONINTERACTIVE=1" in env_text
    assert "LUMEN_UPDATE_BUILD=0" in env_text
    assert "LUMEN_UPDATE_CHANNEL=pinned" in env_text
    assert "LUMEN_UPDATE_FORCE_REDEPLOY=1" in env_text
    assert "LUMEN_REPO_DIR=/root/Lumen" in env_text
    assert "LUMEN_IMAGE_TAG=v1.2.3" in env_text
    assert "HTTP_PROXY=http://proxy.example:3128" in env_text
    # Non-allowlisted vars must not leak into the runner env file.
    assert "PATH=" not in env_text

    trigger_text = (backup_root / ".update.trigger").read_text(encoding="utf-8")
    assert trigger_text.startswith("2026-05-02T00:00:00")


def test_write_marker_tolerates_chmod_eperm_on_squashed_mount(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """In production /opt/lumendata is mounted CIFS with forceuid+file_mode;

    any chmod returns EPERM regardless of who creates the file. _write_marker
    used to bare ``os.chmod`` and crashed trigger_update with HTTP 500. After
    the fix it must succeed even when chmod is unavailable — the marker file
    is what _start_update_via_path_unit relies on as the "an update is in
    progress" signal, so silently dropping it is the worst possible outcome.
    """
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(admin_update.settings, "backup_root", str(backup_root))

    def fake_chmod_eperm(path: object, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(admin_update.os, "chmod", fake_chmod_eperm)

    admin_update._write_marker(
        12345, "2026-05-04T00:00:00+00:00", unit="lumen-update-runner.service"
    )

    marker = backup_root / ".update.running"
    assert marker.is_file()
    text = marker.read_text(encoding="utf-8")
    assert "pid=12345" in text
    assert "unit=lumen-update-runner.service" in text


def test_read_marker_drops_stale_pid_only_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    monkeypatch.setattr(admin_update.settings, "backup_root", str(backup_root))
    monkeypatch.setattr(admin_update, "_pid_is_running", lambda _pid: True)
    marker = backup_root / ".update.running"
    marker.write_text(
        "pid=12345\nstarted_at=2020-01-01T00:00:00+00:00\n",
        encoding="utf-8",
    )

    assert admin_update._read_marker() is None
    assert not marker.exists()


def test_read_marker_keeps_unit_marker_in_trigger_only_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Containerised API cannot query host systemd, so the marker is authoritative."""
    backup_root = tmp_path / "backup"
    backup_root.mkdir()
    monkeypatch.setattr(admin_update.settings, "backup_root", str(backup_root))
    monkeypatch.setenv("LUMEN_UPDATE_VIA_TRIGGER", "1")

    def _boom(_unit: str) -> bool:  # pragma: no cover - guard
        raise AssertionError("must not query host systemd in trigger-only mode")

    monkeypatch.setattr(admin_update, "_unit_is_running", _boom)
    marker = backup_root / ".update.running"
    started_at = datetime.now(timezone.utc).isoformat()
    marker.write_text(
        f"pid=0\nstarted_at={started_at}\nunit=lumen-update-runner.service\n",
        encoding="utf-8",
    )

    read = admin_update._read_marker()

    assert read is not None
    assert read.unit == "lumen-update-runner.service"
    assert marker.exists()


def test_start_update_via_path_unit_tolerates_chmod_eperm_for_env_and_trigger(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The trigger and env files _start_update_via_path_unit writes also live

    on the squashing CIFS mount. Each of those chmod calls used to surface
    EPERM and abort the trigger before the runner could see anything.
    """
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(admin_update.settings, "backup_root", str(backup_root))
    monkeypatch.setenv("LUMEN_UPDATE_VIA_TRIGGER", "1")

    def fake_chmod_eperm(path: object, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(admin_update.os, "chmod", fake_chmod_eperm)
    monkeypatch.setattr(
        admin_update,
        "_wait_for_log_append",
        lambda *args, **kwargs: True,
    )

    log_path = backup_root / ".update.log"
    log_path.parent.mkdir(parents=True)
    with log_path.open("a", encoding="utf-8") as log_fh:
        outcome = admin_update._start_update_via_path_unit(
            env={"LUMEN_UPDATE_NONINTERACTIVE": "1"},
            log_fh=log_fh,
            started_at=datetime(2026, 5, 4, tzinfo=timezone.utc),
        )

    assert outcome == (0, admin_update._UPDATE_RUNNER_UNIT)
    assert (backup_root / ".update.running").is_file()
    assert (backup_root / ".update.env").is_file()
    assert (backup_root / ".update.trigger").is_file()


def test_runner_unit_available_short_circuits_in_trigger_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In docker-compose deploys lumen-api has no systemctl client at all.

    LUMEN_UPDATE_VIA_TRIGGER=1 must make _runner_unit_available() return True
    without trying to spawn systemctl — otherwise the trigger path is
    permanently disabled and the trigger_update endpoint falls through to a
    detached subprocess that can't actually run docker compose from inside
    the api container.
    """
    monkeypatch.setenv("LUMEN_UPDATE_VIA_TRIGGER", "1")

    def _boom(*args: object, **kwargs: object) -> object:  # pragma: no cover - guard
        raise AssertionError("must not invoke systemctl in trigger-only mode")

    monkeypatch.setattr(admin_update.shutil, "which", _boom)
    monkeypatch.setattr(admin_update.subprocess, "run", _boom)

    assert admin_update._runner_unit_available() is True


def test_start_update_via_path_unit_uses_log_confirmation_in_trigger_only_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Containerised lumen-api can't poll _unit_is_running.

    With LUMEN_UPDATE_VIA_TRIGGER=1 set, the function confirms the host path
    watcher started by waiting for update.sh output to appear in the shared log.
    It must not call systemctl or rely on _unit_is_running.
    """
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(admin_update.settings, "backup_root", str(backup_root))
    monkeypatch.setenv("LUMEN_UPDATE_VIA_TRIGGER", "1")

    monkeypatch.setattr(
        admin_update,
        "_wait_for_log_append",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        admin_update,
        "_unit_is_running",
        lambda _unit: pytest.fail("must not poll host systemd in trigger-only mode"),
    )

    log_path = backup_root / ".update.log"
    log_path.parent.mkdir(parents=True)
    with log_path.open("a", encoding="utf-8") as log_fh:
        outcome = admin_update._start_update_via_path_unit(
            env={"LUMEN_UPDATE_NONINTERACTIVE": "1"},
            log_fh=log_fh,
            started_at=datetime(2026, 5, 4, tzinfo=timezone.utc),
        )

    assert outcome == (0, admin_update._UPDATE_RUNNER_UNIT)
    assert (backup_root / ".update.trigger").is_file()
    assert (backup_root / ".update.env").is_file()
    assert (backup_root / ".update.running").is_file()


def test_start_update_via_path_unit_cleans_up_when_trigger_only_runner_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A missing/misconfigured host path unit must not look like success.

    Previously trigger-only mode returned accepted immediately after writing
    .update.trigger. If lumen-update.path was not installed, the UI stayed
    "running" while no update process existed.
    """
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(admin_update.settings, "backup_root", str(backup_root))
    monkeypatch.setenv("LUMEN_UPDATE_VIA_TRIGGER", "1")
    monkeypatch.setattr(
        admin_update,
        "_wait_for_log_append",
        lambda *args, **kwargs: False,
    )

    log_path = backup_root / ".update.log"
    log_path.parent.mkdir(parents=True)
    with log_path.open("a", encoding="utf-8") as log_fh:
        outcome = admin_update._start_update_via_path_unit(
            env={"LUMEN_UPDATE_NONINTERACTIVE": "1"},
            log_fh=log_fh,
            started_at=datetime(2026, 5, 4, tzinfo=timezone.utc),
        )

    assert outcome is None
    assert not (backup_root / ".update.trigger").exists()
    assert not (backup_root / ".update.env").exists()
    assert not (backup_root / ".update.running").exists()
    assert "host runner did not append output" in log_path.read_text(encoding="utf-8")


def test_start_update_via_path_unit_cleans_up_when_runner_does_not_activate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    monkeypatch.setattr(admin_update.settings, "backup_root", str(backup_root))

    # Runner never goes active; shrink the wait so the test stays fast.
    monkeypatch.setattr(admin_update, "_unit_is_running", lambda unit: False)
    counter = {"n": 0}

    def fake_monotonic() -> float:
        counter["n"] += 1
        return float(counter["n"])

    def fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(admin_update.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(admin_update.time, "sleep", fake_sleep)

    log_path = backup_root / ".update.log"
    log_path.parent.mkdir(parents=True)
    with log_path.open("a", encoding="utf-8") as log_fh:
        outcome = admin_update._start_update_via_path_unit(
            env={"LUMEN_UPDATE_NONINTERACTIVE": "1"},
            log_fh=log_fh,
            started_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
        )

    assert outcome is None
    # Staged files cleaned so the next attempt isn't blocked by stale state.
    assert not (backup_root / ".update.running").exists()
    assert not (backup_root / ".update.trigger").exists()
    assert not (backup_root / ".update.env").exists()


@pytest.mark.asyncio
async def test_cleanup_marker_when_done_uses_marker_dataclass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlinked = Mock()
    proc = Mock()
    proc.pid = 1234
    proc.wait.return_value = 0

    monkeypatch.setattr(
        admin_update,
        "_read_marker",
        lambda: admin_update.UpdateMarker(pid=1234, started_at="2026-05-02T00:00:00Z"),
    )
    monkeypatch.setattr(
        admin_update, "_update_marker_path", lambda: Mock(unlink=unlinked)
    )

    await admin_update._cleanup_marker_when_done(proc)

    unlinked.assert_called_once()


@pytest.mark.asyncio
async def test_resolve_update_proxy_uses_named_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_setting(_db: Any, spec: Any) -> str | None:
        values = {
            "update.use_proxy_pool": "1",
            "update.proxy_name": "egress",
        }
        return values.get(spec.key)

    async def fake_load_proxies(_db: Any):
        from lumen_core.providers import ProviderProxyDefinition

        return [
            ProviderProxyDefinition(
                name="egress",
                protocol="socks5",
                host="127.0.0.1",
                port=1080,
                enabled=True,
            )
        ]

    async def fake_resolve(proxy):
        assert proxy.name == "egress"
        return "socks5h://127.0.0.1:1080"

    monkeypatch.setattr(admin_update, "get_setting", fake_get_setting)
    monkeypatch.setattr(admin_update, "_load_proxies", fake_load_proxies)
    monkeypatch.setattr(admin_update, "resolve_provider_proxy_url", fake_resolve)

    proxy, proxy_url = await admin_update._resolve_update_proxy(object())  # type: ignore[arg-type]

    assert proxy is not None
    assert proxy.name == "egress"
    assert proxy_url == "socks5h://127.0.0.1:1080"


@pytest.mark.asyncio
async def test_resolve_update_proxy_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_get_setting(_db: Any, spec: Any) -> str | None:
        return "0" if spec.key == "update.use_proxy_pool" else None

    monkeypatch.setattr(admin_update, "get_setting", fake_get_setting)

    proxy, proxy_url = await admin_update._resolve_update_proxy(object())  # type: ignore[arg-type]

    assert proxy is None
    assert proxy_url is None


@pytest.mark.asyncio
async def test_update_check_minor_channel_uses_current_tag_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "lumen"
    root.mkdir()
    (root / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    shared_env = tmp_path / "shared.env"
    shared_env.write_text("LUMEN_IMAGE_TAG=v1.2.3\n", encoding="utf-8")
    monkeypatch.setenv("LUMEN_SHARED_ENV", str(shared_env))

    async def fail_fetch_latest(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("minor channel must not call GitHub latest release")

    class Redis:
        async def get(self, _key: str) -> None:
            return None

        async def set(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(GitHubReleasesClient, "fetch_latest", fail_fetch_latest)

    out = await UpdateCheckService(root=root, redis=Redis(), ttl_sec=0).check(
        channel="minor",
    )

    assert out.resolved_image_tag == "v1.2"
    assert out.latest_version == "v1.2"
    assert out.has_update is True
    assert out.warning is None
