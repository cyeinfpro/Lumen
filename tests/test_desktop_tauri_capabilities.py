from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESKTOP_ROOT = ROOT / "apps" / "desktop"
TAURI_CONF = DESKTOP_ROOT / "tauri.conf.json"
RUNTIME_TS = ROOT / "apps" / "web" / "src" / "lib" / "desktop" / "runtime.ts"
STARTUP_HTML = DESKTOP_ROOT / "packaging" / "startup" / "index.html"
REMOTE_CAPABILITY = DESKTOP_ROOT / "capabilities" / "main-desktop-bridge.json"
STARTUP_CAPABILITY = DESKTOP_ROOT / "capabilities" / "main-startup-bridge.json"
DESKTOP_COMMANDS = DESKTOP_ROOT / "permissions" / "desktop-commands.toml"


def _permission_id(command: str) -> str:
    return f"allow-{command.replace('_', '-')}"


def _runtime_invoke_commands() -> set[str]:
    text = RUNTIME_TS.read_text(encoding="utf-8")
    return set(
        re.findall(r'desktopInvoke(?:<[^>]+>)?\(\s*"([a-z0-9_]+)"', text)
    )


def _startup_invoke_commands() -> set[str]:
    text = STARTUP_HTML.read_text(encoding="utf-8")
    return set(re.findall(r'invoke\("([a-z0-9_]+)"', text))


def _capability(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _desktop_permissions_by_id() -> dict[str, set[str]]:
    parsed = tomllib.loads(DESKTOP_COMMANDS.read_text(encoding="utf-8"))
    return {
        item["identifier"]: set(item.get("commands", {}).get("allow", []))
        for item in parsed["permission"]
    }


def test_remote_desktop_capability_allows_all_runtime_invokes_on_loopback() -> None:
    commands = _runtime_invoke_commands()
    capability = _capability(REMOTE_CAPABILITY)
    permissions = set(capability["permissions"])

    assert commands == {
        "check_desktop_update",
        "clear_failed_docker_import_marker",
        "clear_failed_restore_marker",
        "clear_pending_restore",
        "desktop_docker_import_status",
        "desktop_restore_status",
        "desktop_status",
        "export_desktop_backup",
        "export_diagnostics_bundle",
        "install_desktop_update",
        "open_data_dir",
        "refresh_provider_runtime",
        "restart_desktop_app",
        "select_desktop_restore_backup",
        "select_docker_import_backup",
        "set_provider_key",
        "set_proxy_secret",
    }
    assert capability["windows"] == ["main"]
    assert capability["local"] is False
    assert capability["remote"] == {"urls": ["http://127.0.0.1:*"]}
    assert "http://localhost:*" not in json.dumps(capability)
    assert {_permission_id(command) for command in commands} <= permissions
    assert "core:event:allow-listen" in permissions
    assert "core:event:allow-unlisten" in permissions


def test_desktop_command_permissions_back_runtime_invokes() -> None:
    permissions_by_id = _desktop_permissions_by_id()

    for command in _runtime_invoke_commands():
        assert permissions_by_id[_permission_id(command)] == {command}


def test_local_startup_capability_preserves_startup_page_invokes() -> None:
    commands = _startup_invoke_commands()
    capability = _capability(STARTUP_CAPABILITY)

    assert commands == {
        "desktop_startup_status",
        "export_diagnostics_bundle",
        "open_data_dir",
        "restart_desktop_app",
        "retry_desktop_startup",
    }
    assert capability["windows"] == ["main"]
    assert capability["local"] is True
    assert "remote" not in capability
    assert set(capability["permissions"]) == {_permission_id(command) for command in commands}


def test_tauri_config_references_only_desktop_capability_files() -> None:
    config = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    security = config["app"]["security"]

    assert security["capabilities"] == [
        "main-desktop-bridge",
        "main-startup-bridge",
    ]
    assert "http://127.0.0.1:*" in security["csp"]
    assert "http://localhost:*" not in security["csp"]
    assert "object-src 'none'" in security["csp"]
    assert "frame-ancestors 'none'" in security["csp"]
