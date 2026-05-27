from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
WORKFLOWS = ROOT / ".github" / "workflows"
DESKTOP_RELEASE = WORKFLOWS / "desktop-release.yml"
COMPOSE = ROOT / "docker-compose.yml"
BLUEGREEN_COMPOSE = ROOT / "docker-compose.bluegreen.yml"
STORAGE_MOUNT = ROOT / "deploy" / "scripts" / "lumen_storage_mount.sh"
FIX_REDIS_PASSWORD = ROOT / "scripts" / "fix-redis-password-mismatch.sh"
SHIFT_TRAFFIC = ROOT / "scripts" / "lumen-shift-traffic.sh"
BUILD_MAC = ROOT / "apps" / "desktop" / "packaging" / "scripts" / "build-mac.sh"
BUILD_WIN = ROOT / "apps" / "desktop" / "packaging" / "scripts" / "build-win.ps1"
PYINSTALLER_API_SPEC = (
    ROOT / "apps" / "desktop" / "packaging" / "pyinstaller" / "lumen-api.spec"
)
PYINSTALLER_WORKER_SPEC = (
    ROOT / "apps" / "desktop" / "packaging" / "pyinstaller" / "lumen-worker.spec"
)
SMOKE_MAC = ROOT / "apps" / "desktop" / "packaging" / "scripts" / "smoke-mac.sh"
SMOKE_WIN = ROOT / "apps" / "desktop" / "packaging" / "scripts" / "smoke-win.ps1"
TAURI_CONF = ROOT / "apps" / "desktop" / "tauri.conf.json"
DESKTOP_SIDECAR_RS = ROOT / "apps" / "desktop" / "src" / "sidecar.rs"
DESKTOP_MAIN_RS = ROOT / "apps" / "desktop" / "src" / "main.rs"
DESKTOP_POWER_RS = ROOT / "apps" / "desktop" / "src" / "power.rs"
DESKTOP_DOCKER_IMPORT_RS = ROOT / "apps" / "desktop" / "src" / "docker_import.rs"
API_DESKTOP_ROUTES = ROOT / "apps" / "api" / "app" / "routes" / "desktop.py"
API_MAIN = ROOT / "apps" / "api" / "app" / "main.py"
WEB_PROXY = ROOT / "apps" / "web" / "src" / "proxy.ts"
WEB_COMMAND_PALETTE = (
    ROOT / "apps" / "web" / "src" / "components" / "ui" / "CommandPalette.tsx"
)


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    return subprocess.run(
        ["bash", "-lc", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def test_safe_rm_rejects_system_and_home_directories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = f"""
    set +e
    export HOME={shlex.quote(str(home))}
    . {shlex.quote(str(LIB))}
    for path in / /opt /opt/ /usr /var "$HOME"; do
        if lumen_path_safe_for_rm "$path"; then
            printf 'unsafe path allowed: %s\\n' "$path" >&2
            exit 1
        fi
    done
    for path in /opt/lumendata /var/lib/lumen-data /srv/lumen-data; do
        if ! lumen_path_safe_for_rm "$path"; then
            printf 'lumen data path rejected: %s\\n' "$path" >&2
            exit 2
        fi
    done
    """

    result = _run_bash(script)

    assert result.returncode == 0, result.stderr + result.stdout


def test_github_workflow_actions_are_pinned_to_commit_sha() -> None:
    floating: list[str] = []
    floating_ref = re.compile(r"uses:\s*[^@\s]+@v\d+(?:\.\d+)?(?:\.\d+)?\b")
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if floating_ref.search(line):
                floating.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")

    assert floating == []


def test_desktop_release_allows_installer_only_artifacts() -> None:
    workflow = DESKTOP_RELEASE.read_text(encoding="utf-8")

    assert "No signed updater artifacts found; skipping latest.json." in workflow
    assert (
        'if [ -z "$mac_update" ] && [ -z "$win_x64_update" ] && [ -z "$win_arm64_update" ]; then'
        in workflow
    )
    assert 'test -n "$mac_update"' in workflow
    assert 'test -n "$win_x64_update"' in workflow
    assert '--artifact "windows-aarch64=$win_arm64_update"' in workflow


def test_desktop_release_builds_windows_arm64_artifact() -> None:
    workflow = DESKTOP_RELEASE.read_text(encoding="utf-8")
    build_win = BUILD_WIN.read_text(encoding="utf-8")

    assert "build-win-arm64:" in workflow
    assert "rustup target add aarch64-pc-windows-msvc" in workflow
    assert "LUMEN_DESKTOP_BUILD_TARGET: aarch64-pc-windows-msvc" in workflow
    assert "apps/desktop/target/aarch64-pc-windows-msvc/release/bundle/nsis" in workflow
    assert "$BuildTarget = if ($env:LUMEN_DESKTOP_BUILD_TARGET)" in build_win
    assert 'cargoArgs += @("--target", $BuildTarget)' in build_win
    assert "function Test-TargetRunsOnHost" in build_win
    assert "if (Test-TargetRunsOnHost)" in build_win
    assert (
        "Skipping executable Node runtime check for cross-architecture target"
        in build_win
    )


def test_desktop_mac_release_requires_valid_bundle_signature() -> None:
    build_mac = BUILD_MAC.read_text(encoding="utf-8")
    smoke_mac = SMOKE_MAC.read_text(encoding="utf-8")

    assert 'export APPLE_SIGNING_IDENTITY="-"' in build_mac
    assert "verify_macos_dmg_bundle_signature" in build_mac
    assert (
        'hdiutil attach "$dmg" -nobrowse -readonly -mountpoint "$mount" -quiet'
        in build_mac
    )
    assert 'codesign --verify --deep --strict --verbose=2 "$app"' in build_mac
    assert 'codesign --verify --deep --strict --verbose=2 "$app"' in smoke_mac


def test_desktop_web_runtime_is_resource_backed_not_placeholder_external_bin() -> None:
    tauri_conf = TAURI_CONF.read_text(encoding="utf-8")
    build_mac = BUILD_MAC.read_text(encoding="utf-8")
    build_win = BUILD_WIN.read_text(encoding="utf-8")

    assert '"externalBin"' not in tauri_conf
    assert "resources/runtime/**/*" in tauri_conf
    assert "resources/web/**/*" in tauri_conf
    for text in (build_mac, build_win):
        assert "cargo build --release --bin lumen-web" not in text
        assert "binaries/lumen-web" not in text


def test_windows_desktop_sidecars_do_not_open_console_windows() -> None:
    for path in (DESKTOP_SIDECAR_RS, DESKTOP_DOCKER_IMPORT_RS):
        text = path.read_text(encoding="utf-8")
        assert "CREATE_NO_WINDOW" in text
        assert "creation_flags(CREATE_NO_WINDOW)" in text


def test_windows_rss_probe_uses_pointer_null_check() -> None:
    text = DESKTOP_SIDECAR_RS.read_text(encoding="utf-8")

    assert "let handle = OpenProcess(" in text
    assert "handle.is_null()" in text
    assert "handle == 0" not in text


def test_desktop_runtime_logs_are_rotated() -> None:
    text = DESKTOP_SIDECAR_RS.read_text(encoding="utf-8")

    assert "DESKTOP_LOG_ROTATE_BYTES" in text
    assert "DESKTOP_LOG_ROTATE_KEEP" in text
    assert "open_rotated_log_file" in text
    assert "rotate_log_if_needed(&path)" in text
    assert 'open_rotated_log_file(&self.runtime.data_root, "supervisor.log")' in text
    assert "log_sequence: Arc<AtomicU64>" in text
    assert '"sequence": sequence' in text


def test_desktop_sidecars_allow_packaged_tiktoken_to_warm_before_estimate_mode() -> (
    None
):
    text = DESKTOP_SIDECAR_RS.read_text(encoding="utf-8")

    assert "LUMEN_TIKTOKEN_LOAD_TIMEOUT_SEC" in text
    assert 'unwrap_or_else(|_| "2.0".to_string())' in text


def test_desktop_redis_runtime_requires_lua_eval() -> None:
    sidecar = DESKTOP_SIDECAR_RS.read_text(encoding="utf-8")
    smoke_mac = SMOKE_MAC.read_text(encoding="utf-8")
    smoke_win = SMOKE_WIN.read_text(encoding="utf-8")

    assert '.arg("--lua")' in sidecar
    assert "EVAL\\r\\n$8\\r\\nreturn 1" in sidecar
    assert "redis lua eval failed" in sidecar
    for text in (smoke_mac, smoke_win):
        assert "Lua scripting support disabled" in text
        assert "redis lua scripting is disabled" in text
        assert "redis lua xadd fallback did not handle Garnet" in text
        assert "redis stream xadd fallback did not handle Garnet" in text


def test_desktop_packaging_verifies_bundled_runtime_resources() -> None:
    build_mac = BUILD_MAC.read_text(encoding="utf-8")
    build_win = BUILD_WIN.read_text(encoding="utf-8")

    for text in (build_mac, build_win):
        assert "server.js" in text
        assert "resources/runtime/node" in text
        assert "resources/runtime/lumen-api" in text
        assert "resources/runtime/lumen-worker" in text
        assert "resources/runtime/lumen-redis" in text
        assert "resources/runtime/dotnet" in text
    assert "verify_desktop_resources" in build_mac
    assert "Verify-DesktopResources" in build_win


def test_desktop_pyinstaller_bundles_tiktoken_encoding_plugins() -> None:
    for path in (PYINSTALLER_API_SPEC, PYINSTALLER_WORKER_SPEC):
        text = path.read_text(encoding="utf-8")
        assert "collect_submodules" in text
        assert 'collect_submodules("tiktoken_ext")' in text


def test_desktop_mac_uses_current_garnet_asset_names() -> None:
    build_mac = BUILD_MAC.read_text(encoding="utf-8")

    assert 'asset="osx-arm64-based.tar.xz"' in build_mac
    assert 'asset="osx-x64-based.tar.xz"' in build_mac
    assert "osx-arm64-based-readytorun.tar.xz" not in build_mac
    assert "osx-x64-based-readytorun.tar.xz" not in build_mac


def test_desktop_mac_smoke_embedded_python_stays_system_compatible() -> None:
    smoke_mac = SMOKE_MAC.read_text(encoding="utf-8")

    assert " | None" not in smoke_mac
    assert "dict[str" not in smoke_mac
    assert "list[int" not in smoke_mac


def test_desktop_smoke_verifies_local_api_token_boundary() -> None:
    api_main = API_MAIN.read_text(encoding="utf-8")
    web_proxy = WEB_PROXY.read_text(encoding="utf-8")
    smoke_mac = SMOKE_MAC.read_text(encoding="utf-8")
    smoke_win = SMOKE_WIN.read_text(encoding="utf-8")

    assert "_DesktopLocalTokenMiddleware" in api_main
    assert '"/system/desktop-ready"' in api_main
    assert 'requestHeaders.set("x-lumen-local-token", token)' in web_proxy
    for text in (smoke_mac, smoke_win):
        assert "/auth/me" in text
        assert "/system/desktop-activity" in text
        assert "/api/system/desktop-activity" in text
        assert "without desktop token did not return 401" in text


def test_desktop_headless_smoke_covers_command_backing_operations() -> None:
    main_rs = DESKTOP_MAIN_RS.read_text(encoding="utf-8")
    smoke_mac = SMOKE_MAC.read_text(encoding="utf-8")
    smoke_win = SMOKE_WIN.read_text(encoding="utf-8")

    for snippet in [
        "run_headless_command_smoke(&mut supervisor)",
        "secrets::set_provider_key",
        "secrets::set_proxy_password",
        "refresh_provider_runtime()",
        "diagnostics::create_diagnostic_bundle",
        "backup::create_desktop_backup",
        "backup::pending_restore_status",
        "docker_import::pending_docker_import_status",
        "headless-command-smoke-ok.json",
        "desktop_headless_command_smoke_ok",
    ]:
        assert snippet in main_rs

    for text in (smoke_mac, smoke_win):
        assert "LUMEN_DATA_ROOT" in text
        assert "LUMEN_DESKTOP_HEADLESS_SMOKE" in text
        assert "headless-command-smoke-ok.json" in text
        assert "desktop headless command smoke marker was not written" in text
        assert "desktop headless command smoke marker payload was invalid" in text


def test_desktop_packaged_smoke_covers_local_routes_and_crud() -> None:
    web_proxy = WEB_PROXY.read_text(encoding="utf-8")
    smoke_mac = SMOKE_MAC.read_text(encoding="utf-8")
    smoke_win = SMOKE_WIN.read_text(encoding="utf-8")

    desktop_routes = [
        "/assets",
        "/stream",
        "/me",
        "/settings/providers",
        "/settings/storage",
        "/settings/diagnostics",
        "/settings/update",
        "/settings/memory",
        "/settings/prompts",
    ]
    docker_only_routes = [
        "/admin",
        "/login",
        "/library",
        "/poster-styles",
        "/projects",
        "/me/wallet",
        "/settings/api-key",
        "/settings/privacy",
        "/settings/telegram",
        "/settings/usage",
    ]
    api_routes = [
        "/api/auth/csrf",
        "/api/auth/logout",
        "/api/settings/bootstrap-status",
        "/api/settings/diagnostics",
        "/api/settings/system",
        "/api/settings/providers",
        "/api/settings/providers/probe",
        "/api/settings/providers/stats",
        "/api/conversations",
        "/api/generations/feed",
        "/api/images/upload",
        "/api/images/",
        "/api/images/share",
        "/api/me/shares",
        "/api/share/",
        "/api/shares/",
        "/api/system-prompts",
        "/api/me/memory-settings",
        "/api/me/memory-scopes",
        "/api/me/onboarding-seen",
        "/api/me/memories",
        "/api/me/memories/staging",
        "/api/me/memories/timeline",
    ]

    assert "DESKTOP_UNSUPPORTED_PREFIXES" in web_proxy
    for route in docker_only_routes:
        assert f'"{route}"' in web_proxy

    for text in (smoke_mac, smoke_win):
        for route in desktop_routes:
            assert route in text
        for route in docker_only_routes:
            assert route in text
        for route in api_routes:
            assert route in text
        assert "desktop unsupported route" in text
        assert "desktop bootstrap-complete did not return complete=true" in text
        assert "desktop csrf did not return desktop-local-token" in text
        assert "desktop logout returned" in text
        assert "desktop auth/me failed after logout no-op" in text
        assert "desktop diagnostics payload did not match runtime state" in text
        assert "desktop settings/system PUT" in text
        assert "desktop settings/system unsupported key returned" in text
        assert "desktop settings/system invalid value returned" in text
        assert "desktop conversation create did not return an id" in text
        assert "desktop conversation patch did not persist title" in text
        assert "desktop conversation delete did not return ok=true" in text
        assert "desktop system prompt create did not return a default prompt" in text
        assert "desktop system prompt patch did not persist name" in text
        assert "desktop system prompt delete returned" in text
        assert "desktop providers PUT did not persist masked provider" in text
        assert "desktop providers probe did not skip generation-locked provider" in text
        assert "desktop provider enabled PATCH did not persist false" in text
        assert "desktop providers clear did not return empty items" in text
        assert "endpoint_locked_to_generations" in text
        assert "redis lua scripting is disabled" in text
        assert "desktop generations feed did not return an item list" in text
        assert "desktop generations feed invalid ratio returned" in text
        assert "desktop image upload did not return expected metadata" in text
        assert "desktop image metadata did not include normalized_ref" in text
        assert "desktop image binary did not return 200" in text
        assert "desktop image display variant did not return 200" in text
        assert "desktop image share create did not return a token" in text
        assert "desktop share list did not include created share" in text
        assert "desktop public share metadata did not include uploaded image" in text
        assert "desktop public share metadata did not include display variant" in text
        assert "desktop public share display variant did not return 200" in text
        assert "desktop public share image did not return 200" in text
        assert "desktop public share image-by-id did not return 200" in text
        assert "desktop public share invalid variant did not return 400" in text
        assert "desktop share revoke returned" in text
        assert "desktop revoked share did not return 404" in text
        assert "desktop multi-image share create did not return image_ids" in text
        assert "desktop multi-image public image-by-id did not return 200" in text
        assert "desktop multi-image share revoke returned" in text
        assert "desktop image delete did not return ok=true" in text
        assert "desktop image binary after delete did not return 404" in text
        assert "desktop image and feed requests failed" in text
        assert "desktop memory settings patch did not persist" in text
        assert "desktop memory onboarding flag did not persist" in text
        assert "desktop memory scope patch did not persist" in text
        assert "desktop conversation active memory scope did not persist" in text
        assert "desktop conversation memory disable did not persist" in text
        assert "desktop conversation used memories did not return 200" in text
        assert "desktop memory create did not return a pinned memory" in text
        assert "desktop memory patch did not persist" in text
        assert "desktop memory scope assignment did not persist" in text
        assert "desktop memory confirm did not persist" in text
        assert "desktop memories filtered list did not include saved memory" in text
        assert "desktop memory staging list did not return 200" in text
        assert "desktop memory timeline did not include audit rows" in text
        assert "desktop memory delete did not return ok=true" in text
        assert "desktop memory clear did not delete rows" in text
        assert "desktop memory scope delete did not return moved count" in text
        assert "desktop share page did not return 200" in text
    assert "HTTP_TIMEOUT_SECONDS = 8" in smoke_mac
    assert "$httpTimeoutSec = 8" in smoke_win
    assert "time.sleep(2.0)" in smoke_mac
    assert "Start-Sleep -Seconds 2" in smoke_win


def test_desktop_packaged_smoke_rejects_tiktoken_fallbacks() -> None:
    smoke_mac = SMOKE_MAC.read_text(encoding="utf-8")
    smoke_win = SMOKE_WIN.read_text(encoding="utf-8")

    for text in (smoke_mac, smoke_win):
        assert "context_window.tiktoken_unavailable" in text
        assert "context_window.tiktoken_loading_slow" in text
        assert "packaged Python runtime could not load tiktoken" in text
        assert "packaged Python runtime fell back before tiktoken warmed" in text


def test_windows_desktop_smoke_requires_baseline_and_final_api_readiness() -> None:
    smoke_win = SMOKE_WIN.read_text(encoding="utf-8")

    assert "$baselineReady = $false" in smoke_win
    assert "$baselineReady = $true" in smoke_win
    assert "baseline_ready=$($baselineReady.ToString().ToLowerInvariant())" in smoke_win
    assert "baseline desktop readiness was not reached" in smoke_win
    assert "/system/desktop-ready" in smoke_win
    assert "api desktop-ready did not return 200" in smoke_win


def test_desktop_command_palette_hides_docker_only_routes() -> None:
    command_palette = WEB_COMMAND_PALETTE.read_text(encoding="utf-8")

    assert "const DOCKER_ONLY_COMMANDS" in command_palette
    assert "...(IS_DESKTOP_RUNTIME ? [] : DOCKER_ONLY_COMMANDS)" in command_palette
    docker_only = command_palette.split("const DOCKER_ONLY_COMMANDS", 1)[1]
    assert 'href: "/settings/usage"' in docker_only
    assert 'href: "/settings/privacy"' in docker_only
    assert 'href: "/admin"' in docker_only


def test_desktop_close_to_tray_has_explicit_runtime_shutdown_exit() -> None:
    text = DESKTOP_MAIN_RS.read_text(encoding="utf-8")

    assert "TrayIconBuilder::with_id" in text
    assert "显示 Lumen" in text
    assert "退出 Lumen" in text
    assert "WindowEvent::CloseRequested" in text
    assert "api.prevent_close()" in text
    assert "window.hide()" in text
    assert "fn request_desktop_exit" in text
    assert "guard.shutdown()" in text
    assert "app.exit(0)" in text


def test_desktop_sleep_protection_tracks_running_tasks_only() -> None:
    power = DESKTOP_POWER_RS.read_text(encoding="utf-8")
    main = DESKTOP_MAIN_RS.read_text(encoding="utf-8")
    sidecar = DESKTOP_SIDECAR_RS.read_text(encoding="utf-8")
    api_desktop = API_DESKTOP_ROUTES.read_text(encoding="utf-8")
    route = re.search(
        r"(?ms)^async def desktop_activity\(.*?(?=^@router\.get\()",
        api_desktop,
    )

    assert route is not None
    route_body = route.group(0)
    assert "IOPMAssertionCreateWithName" in power
    assert "SetThreadExecutionState" in power
    assert "caffeinate" not in power
    assert '"/system/desktop-activity"' in api_desktop
    assert "GenerationStatus.RUNNING.value" in route_body
    assert "CompletionStatus.STREAMING.value" in route_body
    assert "GenerationStatus.QUEUED.value" not in route_body
    assert "CompletionStatus.QUEUED.value" not in route_body
    assert "SleepGuard::new()" in main
    assert "sleep_guard.set_active(activity.should_keep_awake())" in main
    assert "X-Lumen-Local-Token" in sidecar


def test_bug_audit_infra_scripts_parse_with_bash_n() -> None:
    result = subprocess.run(
        [
            "bash",
            "-n",
            str(STORAGE_MOUNT),
            str(FIX_REDIS_PASSWORD),
            str(SHIFT_TRAFFIC),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout


def test_compose_healthchecks_are_local_and_hardened() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    bluegreen = BLUEGREEN_COMPOSE.read_text(encoding="utf-8")

    assert "ulimits:" in compose
    assert "LUMEN_ULIMIT_NOFILE_SOFT" in compose
    assert "LUMEN_ULIMIT_NPROC" in compose
    assert "http://127.0.0.1:3000/api/healthz" not in compose
    web = re.search(r"(?ms)^  web:\n(?P<body>.*?)(?=^  \w|\Z)", compose)
    assert web is not None
    assert "wget" not in web.group("body")
    assert "path: '/healthz', method: 'HEAD'" in compose
    assert "health_check_interval=int(os.getenv" in compose
    assert (
        'LUMEN_WORKER_HEALTH_KEY: "${LUMEN_WORKER_HEALTH_KEY:-arq:queue:health-check}"'
        in compose
    )
    assert "redis_client.get(key)" in compose
    assert 'needle = b"\\x00-m\\x00app.main\\x00"' in compose
    assert 'redis.from_url(os.environ["REDIS_URL"]).ping()' in compose
    assert '"${LUMEN_WORKER_DNS_PRIMARY:-1.1.1.1}"' in compose
    assert '"${LUMEN_WORKER_DNS_SECONDARY:-8.8.8.8}"' in compose

    assert "api-green:" in bluegreen
    assert "init: true" in bluegreen
    assert "LUMEN_ULIMIT_NOFILE_SOFT" in bluegreen


def test_compose_one_shot_profiles_do_not_auto_restart() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    for service in ("migrate", "bootstrap"):
        match = re.search(rf"(?ms)^  {service}:\n(?P<body>.*?)(?=^  \w|\Z)", compose)
        assert match is not None, f"{service} service missing"
        assert 'restart: "no"' in match.group("body")
        assert "on-failure" not in match.group("body")


def test_storage_mount_cleans_smb_credentials_on_hard_failures() -> None:
    text = STORAGE_MOUNT.read_text(encoding="utf-8")

    assert text.count("trap \"rm -f '$cred'\" RETURN EXIT") == 2
    assert "if mount -t cifs" in text
    assert 'rm -f "$cred"' in text
    assert "trap - EXIT" in text


def test_fix_redis_password_parses_quoted_env_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "REDIS_URL='redis://:new-secret@redis:6379/0'\nREDIS_PASSWORD=\"old-secret\"\n",
        encoding="utf-8",
    )

    result = _run_bash(
        f"""
        docker() {{
          case "$1" in
            inspect) return 1 ;;
            exec)
              [ "$REDISCLI_AUTH" = "new-secret" ] || {{
                printf 'bad auth: %s\\n' "$REDISCLI_AUTH" >&2
                return 1
              }}
              printf 'PONG\\n'
              return 0
              ;;
            *) return 1 ;;
          esac
        }}
        systemctl() {{ return 1; }}
        id() {{
          if [ "${{1:-}}" = "-u" ]; then
            printf '0\\n'
          else
            command id "$@"
          fi
        }}
        export -f docker systemctl id
        LUMEN_SHARED_ENV={shlex.quote(str(env_file))} DRY_RUN=1 \
          bash {shlex.quote(str(FIX_REDIS_PASSWORD))}
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "ping ok" in result.stdout
    assert env_file.read_text(encoding="utf-8").startswith("REDIS_URL='")


def test_shift_traffic_does_not_restore_empty_config_on_first_failure(
    tmp_path: Path,
) -> None:
    nginx_conf = tmp_path / "lumen-upstream.conf"
    nginx = tmp_path / "nginx"
    nginx.write_text(
        '#!/usr/bin/env bash\n[ "$1" = "-t" ] && exit 1\n', encoding="utf-8"
    )
    nginx.chmod(0o755)

    result = _run_bash(
        "LUMEN_NGINX_UPSTREAM_CONF="
        f"{shlex.quote(str(nginx_conf))} "
        f"NGINX_BIN={shlex.quote(str(nginx))} "
        f"bash {shlex.quote(str(SHIFT_TRAFFIC))} green 50"
    )

    assert result.returncode == 1
    assert not nginx_conf.exists()
