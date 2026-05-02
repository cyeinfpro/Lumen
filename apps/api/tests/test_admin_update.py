from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from app.routes import admin_update


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
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        assert env[key] == "socks5h://127.0.0.1:1080"


def test_update_trigger_note_mentions_restart_and_health_check() -> None:
    # The response currently passes the note inline from trigger_update; keep the
    # user-facing contract visible even if that implementation is later refactored.
    source = Path(admin_update.__file__).read_text(encoding="utf-8")
    assert "重启运行进程并执行健康检查" in source


def test_systemd_run_command_uses_separate_unit_and_marker_cleanup(tmp_path: Path) -> None:
    command = admin_update._systemd_run_command(
        unit="lumen-update-test.service",
        root=tmp_path / "lumen",
        script=tmp_path / "lumen" / "scripts" / "update.sh",
        log_path=tmp_path / ".update.log",
        env_file=tmp_path / ".update.env",
        marker_path=tmp_path / ".update.running",
    )

    assert command[:4] == ["systemd-run", "--unit", "lumen-update-test.service", "--collect"]
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
    monkeypatch.setattr(admin_update, "_systemd_unit_name", lambda _started_at: "lumen-update-test.service")

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
                "LUMEN_UPDATE_BUILD": "1",
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
    assert "HTTP_PROXY=http://proxy.example:3128" in env_text
    # Non-allowlisted vars must not leak into the runner env file.
    assert "PATH=" not in env_text

    trigger_text = (backup_root / ".update.trigger").read_text(encoding="utf-8")
    assert trigger_text.startswith("2026-05-02T00:00:00")


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
    monkeypatch.setattr(admin_update, "_update_marker_path", lambda: Mock(unlink=unlinked))

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
