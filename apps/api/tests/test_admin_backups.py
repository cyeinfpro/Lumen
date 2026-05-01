from __future__ import annotations

from pathlib import Path

import pytest

from app.routes import admin_backups


def test_backup_paths_resolve_from_settings_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    backup_root = tmp_path / "backup"
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "backup.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts_dir / "restore.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setattr(admin_backups.settings, "backup_root", str(backup_root))
    monkeypatch.setattr(admin_backups.settings, "lumen_scripts_dir", str(scripts_dir))

    assert admin_backups._backup_root() == backup_root
    assert admin_backups._backup_script() == scripts_dir / "backup.sh"
    assert admin_backups._restore_script() == scripts_dir / "restore.sh"


@pytest.mark.asyncio
async def test_run_script_uses_async_subprocess(tmp_path: Path) -> None:
    script = tmp_path / "backup.sh"
    script.write_text("printf 'backup ok'\n", encoding="utf-8")

    result = await admin_backups._run_script(script, timeout=5)

    assert result.returncode == 0
    assert result.stdout == "backup ok"
    assert result.stderr == ""
