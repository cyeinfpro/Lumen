from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LUMENCTL = ROOT / "scripts" / "lumenctl.sh"
LIB = ROOT / "scripts" / "lib.sh"
SCRIPT_FILES = [
    LIB,
    LUMENCTL,
    ROOT / "scripts" / "install.sh",
    ROOT / "scripts" / "update.sh",
    ROOT / "scripts" / "uninstall.sh",
    ROOT / "scripts" / "backup.sh",
    ROOT / "scripts" / "restore.sh",
]
INSTALL = ROOT / "scripts" / "install.sh"
UPDATE = ROOT / "scripts" / "update.sh"
UNINSTALL = ROOT / "scripts" / "uninstall.sh"
ADMIN_RELEASE = ROOT / "apps" / "api" / "app" / "routes" / "admin_release.py"


def run_bash(script: str, *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    return subprocess.run(
        ["bash", "-lc", script],
        cwd=ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def assert_bash_ok(script: str) -> subprocess.CompletedProcess[str]:
    result = run_bash(script)
    assert result.returncode == 0, result.stderr + result.stdout
    return result


def test_operations_scripts_parse_with_bash_n() -> None:
    result = subprocess.run(
        ["bash", "-n", *map(str, SCRIPT_FILES)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_install_script_uses_docker_compose_full_stack() -> None:
    """
    docker cutover: install.sh 不再启动宿主机 uv/npm 运行时，而是 docker compose 全栈。
    断言 docker compose 流程关键字 + 反断言旧的 systemctl restart / uv sync / npm ci。
    """
    text = INSTALL.read_text(encoding="utf-8")
    # Docker compose 全栈关键字
    assert "Docker Compose 全栈版" in text
    assert "docker compose pull" in text
    assert "docker compose v2" in text
    # lib.sh 提供的 compose helper（直接引用或本地降级包装）
    assert "lumen_compose" in text
    # /opt/lumendata 数据根目录在脚本里有显式准备逻辑
    assert "/opt/lumendata" in text
    # 反断言：不再依赖宿主机 uv sync / npm ci / systemctl restart lumen-*
    assert "uv sync" not in text
    assert "npm ci" not in text
    assert "systemctl restart lumen-api" not in text
    assert "systemctl restart lumen-worker" not in text
    assert "systemctl restart lumen-web" not in text


def test_install_bootstrap_defaults_to_menu_not_auto_update() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert 'args=("menu")' in text
    assert "避免脚本一运行就跳过菜单" in text
    assert 'exec bash "${script_path}" "${args[@]}" </dev/tty' in text


def test_install_failure_cleanup_array_length_is_bash_safe() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert '${#INSTALL_STARTED_SERVICES[@]:-0}' not in text
    assert '${#INSTALL_STARTED_SERVICES[@]}' in text


def test_install_pull_failure_falls_back_to_main_for_default_tag() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "回退到 main 后重试一次" in text
    assert 'env_file_set "${shared_env}" LUMEN_IMAGE_TAG "main"' in text
    assert "fallback main 后仍失败" in text
    assert "main 镜像也未发布 → 使用 --build 本地构建" in text


def test_web_port_defaults_to_public_bind_and_install_migrates_old_env() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    install = INSTALL.read_text(encoding="utf-8")
    assert '"${WEB_BIND_HOST:-0.0.0.0}:3000:3000"' in compose
    assert "WEB_BIND_HOST=0.0.0.0" in env_example
    assert "WEB_BIND_HOST 仍是旧默认 127.0.0.1，自动改为 0.0.0.0" in install
    assert 'env_file_set "${shared_env}" WEB_BIND_HOST "0.0.0.0"' in install


def test_update_migrates_old_web_bind_and_proxy_env() -> None:
    update = UPDATE.read_text(encoding="utf-8")
    lib = LIB.read_text(encoding="utf-8")

    assert 'if lumen_configure_proxy_env "${SHARED_ENV}"' in update
    assert "config_changed_redeploy" in update
    assert 'lumen_set_env_value_in_file "${SHARED_ENV}" WEB_BIND_HOST "0.0.0.0"' in update
    assert 'emit_info check web_bind_host "${CURRENT_WEB_BIND_HOST:-<default>}"' in update
    assert "LUMEN_HTTP_PROXY HTTPS_PROXY HTTP_PROXY" in lib
    assert 'export HTTP_PROXY="${proxy_url}"' in lib
    assert 'export HTTPS_PROXY="${proxy_url}"' in lib


def test_lumen_configure_proxy_env_reads_shared_env(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LUMEN_HTTP_PROXY=http://127.0.0.1:7890\n"
        "NO_PROXY=localhost,127.0.0.1,::1,10.0.0.0/8\n",
        encoding="utf-8",
    )

    result = assert_bash_ok(
        f"""
        . {LIB}
        unset LUMEN_UPDATE_PROXY_URL LUMEN_HTTP_PROXY HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy NO_PROXY no_proxy
        lumen_configure_proxy_env {shlex.quote(str(env_file))} >/tmp/lumen-proxy-test.out
        printf 'proxy=%s\\n' "$(cat /tmp/lumen-proxy-test.out)"
        printf 'http=%s\\n' "$HTTP_PROXY"
        printf 'https=%s\\n' "$HTTPS_PROXY"
        printf 'update=%s\\n' "$LUMEN_UPDATE_PROXY_URL"
        printf 'no_proxy=%s\\n' "$NO_PROXY"
        """
    )

    assert "proxy=http://127.0.0.1:7890" in result.stdout
    assert "http=http://127.0.0.1:7890" in result.stdout
    assert "https=http://127.0.0.1:7890" in result.stdout
    assert "update=http://127.0.0.1:7890" in result.stdout
    assert "no_proxy=localhost,127.0.0.1,::1,10.0.0.0/8" in result.stdout


def test_update_script_requires_release_layout_and_prepares_new_release() -> None:
    """
    docker cutover: release 布局保留，但 prepare 流程从 git clone 改为 rsync 仓库快照
    + shared/.env symlink，不再走 uv.toml 校验。
    """
    text = (ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
    # 仍然要求 release 布局 + shared/.env 复用
    assert 'NEW_RELEASE="${ROOT}/releases/${NEW_ID}"' in text
    assert 'lumen_release_ensure_shared_env "${ROOT}"' in text
    # docker cutover：fetch_release 阶段改 rsync REPO_DIR -> NEW_RELEASE
    assert "rsync_repo_to_release" in text
    assert 'elif [ -n "${CURRENT_RELEASE}" ] && [ -d "${CURRENT_RELEASE}" ]; then' in text
    assert 'REPO_DIR="${CURRENT_RELEASE}"' in text
    assert 'LUMEN_UPDATE_GIT_PULL=1 但 ${REPO_DIR} 不是 git 仓库；使用当前发布物快照继续' in text
    assert "--exclude='/releases/'" in text
    assert "--exclude='/shared/'" in text
    # release/.env 是 -> shared/.env 的 symlink，docker compose 自动识别
    assert 'ln -sfn "${SHARED_ENV}" "${NEW_RELEASE}/.env"' in text
    # 切换走 atomic switch helper
    assert 'lumen_release_atomic_switch "${ROOT}" "${NEW_ID}"' in text
    # 不再依赖宿主机 uv 配置 / git clone 流程
    assert "uv.toml" not in text
    assert "lumen_update_ensure_runtime_can_access_path" not in text


def test_compose_db_env_vars_backfilled_from_database_url(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL='postgresql+asyncpg://alice:pa%24%24@localhost:5432/lumen_prod'\n",
        encoding="utf-8",
    )

    result = assert_bash_ok(
        f"""
        . {LIB}
        lumen_ensure_compose_db_env_vars {env_file}
        """
    )

    assert "已从 DATABASE_URL 补全" in result.stderr
    text = env_file.read_text(encoding="utf-8")
    assert "DB_USER=alice" in text
    assert "DB_PASSWORD='pa$$'" in text
    assert "DB_NAME=lumen_prod" in text


def test_compose_db_env_vars_fail_without_database_url(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PUBLIC_BASE_URL=http://localhost:3000\n", encoding="utf-8")

    result = run_bash(
        f"""
        . {LIB}
        lumen_ensure_compose_db_env_vars {env_file}
        """
    )

    assert result.returncode == 1
    assert "缺少 DB_USER/DB_PASSWORD/DB_NAME" in result.stderr
    assert "无法从 DATABASE_URL 推导" in result.stderr


def test_container_url_migration_dry_run_and_apply_are_allowlisted(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql+asyncpg://alice:secret@localhost:5432/lumen",
                "REDIS_URL=redis://:redis-secret@127.0.0.1:6379/0",
                "LUMEN_BACKEND_URL=http://127.0.0.1:8000",
                "LUMEN_API_BASE=http://localhost:8000",
                "PUBLIC_BASE_URL=http://localhost:8000",
                "CORS_ALLOW_ORIGINS=http://localhost:3000",
                "NEXT_PUBLIC_API_BASE=/api",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    dry_run = assert_bash_ok(
        f"""
        . {LIB}
        lumen_migrate_container_urls {env_file} --dry-run
        """
    )
    before = env_file.read_text(encoding="utf-8")
    assert "DATABASE_URL:" in dry_run.stdout
    assert "@postgres:5432/lumen" in dry_run.stdout
    assert before.startswith("DATABASE_URL=postgresql+asyncpg://alice:secret@localhost")

    applied = assert_bash_ok(
        f"""
        . {LIB}
        lumen_migrate_container_urls {env_file} --apply
        """
    )
    after = env_file.read_text(encoding="utf-8")
    assert "backup=" in applied.stdout
    assert "DATABASE_URL=postgresql+asyncpg://alice:secret@postgres:5432/lumen" in after
    assert "REDIS_URL=redis://:redis-secret@redis:6379/0" in after
    assert "LUMEN_BACKEND_URL=http://api:8000" in after
    assert "LUMEN_API_BASE=http://api:8000" in after
    # Browser/CORS fields are intentionally not touched by the migration helper.
    assert "PUBLIC_BASE_URL=http://localhost:8000" in after
    assert "CORS_ALLOW_ORIGINS=http://localhost:3000" in after
    assert list(tmp_path.glob(".env.bak.*"))


def test_container_url_migration_rejects_unclassified_localhost_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://alice:secret@localhost:5432/lumen\n"
        "INTERNAL_CALLBACK_URL=http://127.0.0.1:9000\n",
        encoding="utf-8",
    )

    result = run_bash(
        f"""
        . {LIB}
        lumen_migrate_container_urls {env_file} --dry-run
        """
    )

    assert result.returncode == 1
    assert "INTERNAL_CALLBACK_URL still contains localhost/127.0.0.1" in result.stderr


def test_install_existing_env_container_url_check_defaults_to_dry_run() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert 'LUMEN_ENV_MIGRATE_CONTAINER_URLS:-dry-run' in text
    assert 'apply|--apply)' in text
    assert "migrate-env-apply" in text
    assert "检测到旧 .env 仍需要容器地址迁移" in text
    assert "LUMEN_ENV_MIGRATE_CONTAINER_URLS=apply" in text


def test_release_shared_env_recovers_from_root_env(tmp_path: Path) -> None:
    deploy_root = tmp_path / "lumen"
    (deploy_root / "shared").mkdir(parents=True)
    root_env = deploy_root / ".env"
    root_env.write_text("DB_USER=lumen_app\nDB_PASSWORD='secret'\nDB_NAME=lumen\n", encoding="utf-8")

    result = assert_bash_ok(
        f"""
        . {LIB}
        lumen_release_ensure_shared_env {deploy_root}
        test -f {deploy_root / "shared" / ".env"}
        test -L {root_env}
        """
    )

    assert "shared/.env 缺失，检测到 ROOT/.env" in result.stderr
    assert (deploy_root / "shared" / ".env").read_text(encoding="utf-8") == (
        "DB_USER=lumen_app\nDB_PASSWORD='secret'\nDB_NAME=lumen\n"
    )


def test_release_shared_env_fails_when_no_env_source(tmp_path: Path) -> None:
    deploy_root = tmp_path / "lumen"
    (deploy_root / "shared").mkdir(parents=True)

    result = run_bash(
        f"""
        . {LIB}
        lumen_release_ensure_shared_env {deploy_root}
        """
    )

    assert result.returncode == 1
    assert "shared/.env 缺失" in result.stderr
    assert "未找到可恢复的 ROOT/.env 或 current/.env" in result.stderr


def test_rollback_script_validates_compose_env_before_compose_up() -> None:
    text = ADMIN_RELEASE.read_text(encoding="utf-8")
    assert '. "$ROOT/current/scripts/lib.sh"' in text
    assert 'lumen_ensure_compose_db_env_vars "$ROOT/current/.env"' in text
    assert "compose env validation failed; rollback continues but containers may be stale" in text
    assert 'cd "$ROOT/current" && docker compose up -d --wait' in text


def _strip_shell_comments(text: str) -> str:
    """
    去掉 bash 行内注释（# 开头或行末），用于反断言时只看实际可执行命令。
    简化版：一行内首个未在引号里的 # 之后视为注释；不做 here-doc / 复杂引号解析，
    对脚本顶部 banner 和 inline 注释足够。
    """
    out_lines: list[str] = []
    for line in text.splitlines():
        in_single = False
        in_double = False
        i = 0
        cut_at = len(line)
        while i < len(line):
            ch = line[i]
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                if i == 0 or line[i - 1] in (" ", "\t"):
                    cut_at = i
                    break
            i += 1
        out_lines.append(line[:cut_at].rstrip())
    return "\n".join(out_lines)


def test_update_script_runs_docker_compose_pull_migrate_up_phases() -> None:
    """
    docker cutover: update.sh 改为 docker compose pull -> start_infra -> migrate_db
    -> switch -> restart_services。不再 systemctl restart lumen-* /
    uv sync / npm ci / 写 systemd unit。
    """
    text = UPDATE.read_text(encoding="utf-8")
    # 关键阶段（::lumen-step:: phase=...）必须存在
    assert "emit_start set_image_tag" in text
    assert "emit_start pull_images" in text
    assert "emit_start start_infra" in text
    assert "emit_start migrate_db" in text
    assert "emit_start switch" in text
    assert "emit_start restart_services" in text
    # 阶段输出协议（phase=set_image_tag / phase=migrate_db 出现在最终日志）
    assert 'lumen_emit_step "phase=$1"' in text
    # docker compose 关键命令
    assert "lumen_compose_in" in text
    assert "--profile migrate run --rm migrate" in text
    assert "up -d --wait postgres redis" in text
    assert "up -d --wait api worker web" in text
    # release 切换走 atomic switch
    assert 'lumen_release_atomic_switch "${ROOT}" "${NEW_ID}"' in text
    # 反断言：脚本注释里可以提"不再 uv sync / npm ci"，但实际可执行命令必须不包含。
    code = _strip_shell_comments(text)
    assert "systemctl restart lumen-api" not in code
    assert "systemctl restart lumen-worker" not in code
    assert "systemctl restart lumen-web" not in code
    assert "lumen_restart_systemd_units" not in code
    assert "uv sync" not in code
    assert "npm ci" not in code
    assert "npm run build" not in code


def test_update_script_supports_optional_local_build_when_env_set() -> None:
    """
    docker cutover §11.3.2: 默认 pull 优先；LUMEN_UPDATE_BUILD=1 才本地构建。
    """
    text = UPDATE.read_text(encoding="utf-8")
    # build 路径必须由 env 显式开启
    assert 'LUMEN_UPDATE_BUILD:-0' in text
    assert "build api worker web" in text
    # build 路径仍走 lumen_compose_in（不直接 systemctl）
    assert 'lumen_compose_in "${NEW_RELEASE}" build api worker web' in text
    assert 'LUMEN_UPDATE_BUILD=1 已完成本地 build，跳过远程 pull' in text


@pytest.mark.skip(
    reason="docker cutover: 宿主机 uv 自动安装路径已删除（API/Worker 由 lumen-api / lumen-worker 镜像提供）"
)
def test_update_installs_missing_uv_to_system_path_before_runtime_home() -> None:
    pass


def test_shared_runtime_health_helpers_cover_api_web_worker() -> None:
    text = LIB.read_text(encoding="utf-8")
    assert "lumen_check_runtime_health()" in text
    assert "http://127.0.0.1:8000/healthz" in text
    assert "http://127.0.0.1:3000/" in text
    assert "lumen_systemd_unit_active lumen-worker.service" in text
    assert "lumen_start_local_runtime()" in text
    assert "lumen_tail_runtime_log \"Worker\"" in text


def test_local_runtime_stops_persisted_pids_before_port_scan() -> None:
    text = LIB.read_text(encoding="utf-8")
    start = text.index("lumen_start_local_runtime()")
    persisted = text.index('lumen_stop_persisted_runtime "${root}"', start)
    api_port_scan = text.index('lumen_prepare_port_for_runtime 8000 "API"', start)
    assert persisted < api_port_scan
    assert 'LUMEN_RUNTIME_STOP_WAIT_SECONDS:-15' in text[persisted:api_port_scan]


def test_shared_root_resolver_handles_release_current_symlink(tmp_path: Path) -> None:
    deploy_root = tmp_path / "lumen"
    release = deploy_root / "releases" / "20260503010101-abcdef0"
    scripts_dir = release / "scripts"
    scripts_dir.mkdir(parents=True)
    (deploy_root / "current").symlink_to(release)

    result = assert_bash_ok(
        f"""
        . {LIB}
        lumen_resolve_repo_root {deploy_root / "current" / "scripts"}
        """
    )
    assert Path(result.stdout.strip()) == deploy_root


def test_lumenctl_runs_lumen_updates_from_resolved_root_with_sudo_on_linux() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        detect_os() {{ printf 'linux\\n'; }}
        ensure_cmd() {{ :; }}
        bash() {{ printf 'bash:%s\\n' "$*"; }}
        lumen_sudo() {{ printf 'sudo:%s\\n' "$*"; }}
        run_lumen_script update.sh
        """
    )
    if os.geteuid() == 0:
        assert "sudo:" not in result.stdout
        assert f"bash:{ROOT / 'scripts' / 'update.sh'}" in result.stdout
    else:
        assert f"sudo:bash {ROOT / 'scripts' / 'update.sh'}" in result.stdout


def test_lumenctl_resolves_scripts_from_current_release(tmp_path: Path) -> None:
    deploy_root = tmp_path / "Lumen"
    release = deploy_root / "releases" / "20260503010101-abcdef0"
    scripts_dir = release / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "update.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (deploy_root / "current").symlink_to(release)

    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        ROOT={deploy_root}
        detect_os() {{ printf 'linux\\n'; }}
        bash() {{ printf 'bash:%s\\n' "$*"; }}
        lumen_sudo() {{ printf 'sudo:%s\\n' "$*"; }}
        run_lumen_script update.sh
        """
    )

    assert f"{deploy_root / 'current' / 'scripts' / 'update.sh'}" in result.stdout
    assert f"{deploy_root / 'scripts' / 'update.sh'}" not in result.stderr


def test_lumenctl_install_bootstraps_from_github_when_install_script_missing(tmp_path: Path) -> None:
    deploy_root = tmp_path / "Lumen"
    downloaded = tmp_path / "downloaded-install.sh"
    deploy_root.mkdir()

    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        ROOT={deploy_root}
        LUMEN_BRANCH=main
        mktemp() {{ printf '%s\\n' {downloaded}; }}
        ensure_cmd() {{ :; }}
        curl() {{
          printf 'curl:%s\\n' "$*"
          printf '#!/usr/bin/env bash\\nexit 0\\n' > "$4"
        }}
        bash() {{ printf 'bash:%s\\n' "$*"; }}
        run_lumen_script install.sh --image-tag=main
        printf 'install_dir:%s\\n' "${{LUMEN_INSTALL_DIR}}"
        """
    )

    assert "raw.githubusercontent.com/cyeinfpro/Lumen/main/scripts/install.sh" in result.stdout
    assert f"bash:{downloaded} --install --image-tag=main" in result.stdout
    assert f"install_dir:{deploy_root}" in result.stdout


def test_runtime_health_check_fails_when_api_unhealthy() -> None:
    result = run_bash(
        f"""
        . {LIB}
        log_step() {{ :; }}
        sleep() {{ :; }}
        curl() {{
          case "$*" in
            *'127.0.0.1:8000/healthz'*) printf '500' ;;
            *'127.0.0.1:3000/'*) printf '200' ;;
            *) printf '000' ;;
          esac
        }}
        systemctl() {{
          case "$1 $2" in
            "list-unit-files lumen-worker.service") printf 'lumen-worker.service enabled\\n' ;;
            "is-active --quiet") return 0 ;;
            *) return 0 ;;
          esac
        }}
        LUMEN_API_HEALTH_ATTEMPTS=1 LUMEN_WEB_HEALTH_ATTEMPTS=1 lumen_check_runtime_health
        """
    )
    assert result.returncode == 1
    assert "API 健康检查失败" in result.stderr


def test_runtime_health_check_passes_for_api_web_and_worker() -> None:
    result = assert_bash_ok(
        f"""
        . {LIB}
        log_step() {{ :; }}
        sleep() {{ :; }}
        curl() {{
          case "$*" in
            *'127.0.0.1:8000/healthz'*) printf '200' ;;
            *'127.0.0.1:3000/'*) printf '200' ;;
            *) printf '000' ;;
          esac
        }}
        systemctl() {{
          case "$1 $2" in
            "list-unit-files lumen-worker.service") printf 'lumen-worker.service enabled\\n' ;;
            "is-active --quiet") return 0 ;;
            *) return 0 ;;
          esac
        }}
        LUMEN_API_HEALTH_ATTEMPTS=1 LUMEN_WEB_HEALTH_ATTEMPTS=1 lumen_check_runtime_health
        """
    )
    assert "API 健康检查通过" in result.stdout
    assert "Web 健康检查通过" in result.stdout


def test_runtime_dir_check_uses_systemd_service_user(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    storage = tmp_path / "storage"
    backup = tmp_path / "backup"
    env_file.write_text(
        f"STORAGE_ROOT={storage}\nBACKUP_ROOT={backup}\n",
        encoding="utf-8",
    )

    result = assert_bash_ok(
        f"""
        . {LIB}
        systemctl() {{
          case "$1 $2" in
            "list-unit-files lumen-api.service") printf 'lumen-api.service enabled\\n' ;;
            "show -p") printf 'lumen\\n' ;;
            *) return 1 ;;
          esac
        }}
        id() {{
          case "$1 $2" in
            "-gn lumen") printf 'lumen\\n' ;;
            "-un ") printf 'tester\\n' ;;
            *) command id "$@" ;;
          esac
        }}
        lumen_run_as_user() {{ shift; "$@"; }}
        lumen_run_as_root() {{ "$@"; }}
        lumen_ensure_runtime_dirs {env_file}
        test -d {storage}
        test -d {backup / "pg"}
        test -d {backup / "redis"}
        """
    )
    assert "运行用户：lumen:lumen" in result.stdout


def test_lumen_with_lock_falls_back_to_mkdir_when_flock_missing(tmp_path: Path) -> None:
    lock_root = tmp_path / "backup"
    result = assert_bash_ok(
        f"""
        . {LIB}
        command() {{
          if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
            return 1
          fi
          builtin command "$@"
        }}
        LUMEN_BACKUP_ROOT={lock_root}
        lumen_with_lock update-test 30 bash -c 'printf locked'
        test ! -d {lock_root / ".lumen-update.lock.d"}
        """
    )
    assert result.stdout == "locked"


def test_lumen_with_lock_mkdir_busy_returns_operation_busy(tmp_path: Path) -> None:
    lock_root = tmp_path / "backup"
    (lock_root / ".lumen-update.lock.d").mkdir(parents=True)

    result = run_bash(
        f"""
        . {LIB}
        command() {{
          if [ "$1" = "-v" ] && [ "${{2:-}}" = "flock" ]; then
            return 1
          fi
          builtin command "$@"
        }}
        LUMEN_BACKUP_ROOT={lock_root}
        lumen_with_lock update-test 30 true
        """
    )

    assert result.returncode == 75
    assert '"code":"system_operation_busy"' in result.stdout


def test_lumenctl_help_lists_every_documented_command() -> None:
    result = subprocess.run(
        ["bash", str(LUMENCTL), "help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    output = result.stdout
    for command in (
        # 旧命令必须保留
        "menu",
        "install-lumen",
        "update-lumen",
        "uninstall-lumen",
        "install-image-job",
        "uninstall-image-job",
        "nginx-scan",
        "nginx-optimize",
        "nginx-lumen",
        "nginx-sub2api",
        "nginx-sub2api-inner",
        "nginx-sub2api-outer",
        "nginx-image-job",
        "help",
        # docker cutover §24 新增的 lifecycle / compose runtime 命令
        "rollback",
        "version",
        "status",
        "logs",
        "start",
        "stop",
        "restart",
        "migrate",
        "bootstrap",
        "backup",
        "restore",
    ):
        assert f"  {command}" in output, f"lumenctl.sh help 缺少子命令：{command}"


def test_lumenctl_menu_accepts_default_exit_without_error() -> None:
    result = subprocess.run(
        ["bash", str(LUMENCTL), "menu"],
        cwd=ROOT,
        input="\n",
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "Lumen 一键运维菜单" in result.stdout


def test_lumenctl_direct_commands_dispatch_to_expected_handlers() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        run_lumen_script() {{ printf 'run_lumen_script:%s %s\\n' "$1" "${{*:2}}"; }}
        install_image_job() {{ printf 'install_image_job\\n'; }}
        uninstall_image_job() {{ printf 'uninstall_image_job\\n'; }}
        nginx_scan() {{ printf 'nginx_scan\\n'; }}
        nginx_optimize() {{ printf 'nginx_optimize\\n'; }}
        nginx_lumen_proxy() {{ printf 'nginx_lumen_proxy\\n'; }}
        nginx_sub2api_proxy() {{ printf 'nginx_sub2api_proxy\\n'; }}
        nginx_sub2api_inner_proxy() {{ printf 'nginx_sub2api_inner_proxy\\n'; }}
        nginx_sub2api_outer_proxy() {{ printf 'nginx_sub2api_outer_proxy\\n'; }}
        nginx_image_job_locations() {{ printf 'nginx_image_job_locations\\n'; }}

        main install-lumen
        main update-lumen
        main uninstall-lumen
        main install-image-job
        main uninstall-image-job
        main nginx-scan
        main nginx-optimize
        main nginx-lumen
        main nginx-sub2api
        main nginx-sub2api-inner
        main nginx-sub2api-outer
        main nginx-image-job
        """
    )
    assert result.stdout.splitlines() == [
        "run_lumen_script:install.sh --install",
        "run_lumen_script:update.sh ",
        "run_lumen_script:uninstall.sh ",
        "install_image_job",
        "uninstall_image_job",
        "nginx_scan",
        "nginx_optimize",
        "nginx_lumen_proxy",
        "nginx_sub2api_proxy",
        "nginx_sub2api_inner_proxy",
        "nginx_sub2api_outer_proxy",
        "nginx_image_job_locations",
    ]


def test_lumenctl_interactive_menu_dispatches_all_numbered_options() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        read_or_default() {{
          local _prompt="$1"
          local default="${{2:-}}"
          local reply=""
          if ! IFS= read -r reply; then
            reply=""
          fi
          printf '%s' "${{reply:-$default}}"
        }}
        run_lumen_script() {{ printf 'run_lumen_script:%s %s\\n' "$1" "${{*:2}}"; }}
        install_image_job() {{ printf 'install_image_job\\n'; }}
        uninstall_image_job() {{ printf 'uninstall_image_job\\n'; }}
        nginx_scan() {{ printf 'nginx_scan\\n'; }}
        nginx_optimize() {{ printf 'nginx_optimize\\n'; }}

        printf '1\\n2\\n3\\n4\\n5\\n6\\n7\\n0\\n' | show_menu
        """
    )
    dispatch_lines = [
        line
        for line in result.stdout.splitlines()
        if line.startswith(("run_lumen_script:", "install_image_job", "uninstall_image_job", "nginx_"))
    ]
    assert dispatch_lines == [
        "run_lumen_script:install.sh --install",
        "run_lumen_script:update.sh ",
        "run_lumen_script:uninstall.sh ",
        "install_image_job",
        "uninstall_image_job",
        "nginx_scan",
        "nginx_optimize",
    ]


def test_lumenctl_install_lumen_preserves_explicit_install_flag() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        run_lumen_script() {{ printf 'run_lumen_script:%s %s\\n' "$1" "${{*:2}}"; }}

        main install-lumen --image-tag=main
        main install-lumen --install --image-tag=main
        """
    )

    assert result.stdout.splitlines() == [
        "run_lumen_script:install.sh --install --image-tag=main",
        "run_lumen_script:install.sh --install --image-tag=main",
    ]


def test_lumenctl_install_update_uninstall_smoke_with_fake_docker(tmp_path: Path) -> None:
    """
    Runs the real lumenctl -> install.sh / update.sh / uninstall.sh entrypoints
    against a temp release layout with fake docker/curl/sudo functions. This
    verifies the one-click control flow without touching the host daemon or /opt.
    """
    sim_root = tmp_path / "sim"
    deploy_root = sim_root / "deploy"
    data_root = sim_root / "data"
    log_dir = sim_root / "logs"
    fakebin = sim_root / "fakebin"
    log_dir.mkdir(parents=True)
    fakebin.mkdir(parents=True)

    (fakebin / "docker").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf 'docker %s\\n' "$*" >> "${LOG_DIR}/docker.log"
if [ "$#" -ge 1 ] && [ "$1" = "--version" ]; then
  printf 'Docker version 26.0.0, build fake\\n'
  exit 0
fi
if [ "$#" -ge 2 ] && [ "$1" = "compose" ] && [ "$2" = "version" ]; then
  printf 'Docker Compose version v2.27.0\\n'
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "info" ]; then
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "compose" ]; then
  shift
  [ "${1:-}" = "--ansi=never" ] && shift
  if [ "${1:-}" = "ps" ]; then
    svc="${*: -1}"
    printf 'cid-%s\\n' "${svc}"
    exit 0
  fi
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "inspect" ]; then
  printf 'healthy\\n'
  exit 0
fi
if [ "$#" -ge 2 ] && [ "$1" = "image" ] && [ "$2" = "prune" ]; then
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "ps" ]; then
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "rm" ]; then
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "cp" ]; then
  dest="${*: -1}"
  mkdir -p "$(dirname "${dest}")"
  printf 'redis-dump\\n' > "${dest}"
  exit 0
fi
if [ "$#" -ge 1 ] && [ "$1" = "exec" ]; then
  args="$*"
  case "${args}" in
    *redis-cli*LASTSAVE*)
      sed -n 's/^lastsave=//p' "${TEST_DOCKER_STATE}" | tail -n1
      exit 0
      ;;
    *redis-cli*BGSAVE*)
      printf 'lastsave=2\\n' > "${TEST_DOCKER_STATE}"
      printf 'Background saving started\\n'
      exit 0
      ;;
    *redis-cli*)
      printf 'OK\\n'
      exit 0
      ;;
    *pg_dump*)
      printf 'fake pg dump\\n'
      exit 0
      ;;
  esac
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    (fakebin / "curl").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
original="$*"
printf 'curl %s\\n' "${original}" >> "${LOG_DIR}/curl.log"
case "${original}" in
  *api.github.com/repos/cyeinfpro/Lumen/releases/latest*)
    printf '{"tag_name":"main"}\\n'
    exit 0
    ;;
  *ghcr.io/token*)
    printf '{"token":"fake"}\\n'
    exit 0
    ;;
esac
out=""
write_code=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o)
      out="$2"
      shift 2
      ;;
    -w)
      write_code="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done
if [ -n "${out}" ] && [ "${out}" != "/dev/null" ]; then
  printf '{"tags":["latest","main","old"]}\\n' > "${out}"
fi
if [ -n "${write_code}" ]; then
  printf '200'
fi
exit 0
""",
        encoding="utf-8",
    )
    (fakebin / "sudo").write_text(
        """#!/usr/bin/env bash
set -euo pipefail
[ "${1:-}" = "-n" ] && shift
if [ "${1:-}" = "-u" ]; then
  shift 2
fi
case "${1:-}" in
  chown)
    exit 0
    ;;
  chmod)
    shift
    command chmod "$@" 2>/dev/null || true
    exit 0
    ;;
  mkdir|rm|ln|mv|cp)
    command "$@"
    ;;
  docker)
    shift
    docker "$@"
    ;;
  *)
    command "$@"
    ;;
esac
""",
        encoding="utf-8",
    )
    (fakebin / "systemctl").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    (fakebin / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    for path in fakebin.iterdir():
        path.chmod(0o755)

    script = f"""
    set -euo pipefail
    SIM_ROOT={shlex.quote(str(sim_root))}
    DEPLOY_ROOT={shlex.quote(str(deploy_root))}
    DATA_ROOT={shlex.quote(str(data_root))}
    LOG_DIR={shlex.quote(str(log_dir))}
    export LOG_DIR
    : > "${{LOG_DIR}}/docker.log"
    : > "${{LOG_DIR}}/curl.log"
    export TEST_DOCKER_STATE="${{SIM_ROOT}}/docker-state"
    mkdir -p "${{SIM_ROOT}}"
    printf 'lastsave=1\\n' > "${{TEST_DOCKER_STATE}}"
    export PATH={shlex.quote(str(fakebin))}:$PATH

    export LUMEN_DEPLOY_ROOT="${{DEPLOY_ROOT}}"
    export LUMEN_DATA_ROOT="${{DATA_ROOT}}"
    export LUMEN_NONINTERACTIVE=1
    export LUMEN_ADMIN_EMAIL=admin@example.com
    export LUMEN_ADMIN_PASSWORD=password123456
    export LUMEN_HEALTH_COMPOSE_ATTEMPTS=1
    export LUMEN_HEALTH_COMPOSE_INTERVAL=1
    export LUMEN_RELEASE_KEEP=3
    export LUMEN_BACKUP_RESTORE_LOCKFILE="${{DATA_ROOT}}/backup/backup-restore.lock"
    export LUMEN_BACKUP_ROOT="${{DATA_ROOT}}/backup"

    bash scripts/lumenctl.sh install-lumen --image-tag=old > "${{LOG_DIR}}/install.out" 2> "${{LOG_DIR}}/install.err"
    test -L "${{DEPLOY_ROOT}}/current"
    test -f "${{DEPLOY_ROOT}}/current/docker-compose.yml"
    test -f "${{DEPLOY_ROOT}}/shared/.env"
    grep -q '^LUMEN_IMAGE_TAG=old$' "${{DEPLOY_ROOT}}/shared/.env"

    if grep -q '^LUMEN_UPDATE_CHANNEL=' "${{DEPLOY_ROOT}}/shared/.env"; then
      sed -i.bak 's/^LUMEN_UPDATE_CHANNEL=.*/LUMEN_UPDATE_CHANNEL=main/' "${{DEPLOY_ROOT}}/shared/.env"
      rm -f "${{DEPLOY_ROOT}}/shared/.env.bak"
    else
      printf 'LUMEN_UPDATE_CHANNEL=main\\n' >> "${{DEPLOY_ROOT}}/shared/.env"
    fi

    LUMEN_UPDATE_GIT_PULL=1 bash "${{DEPLOY_ROOT}}/current/scripts/lumenctl.sh" update-lumen > "${{LOG_DIR}}/update.out" 2> "${{LOG_DIR}}/update.err"
    test -L "${{DEPLOY_ROOT}}/current"
    test -f "${{DEPLOY_ROOT}}/current/docker-compose.yml"
    grep -q '^LUMEN_IMAGE_TAG=main$' "${{DEPLOY_ROOT}}/shared/.env"
    test "$(find "${{DEPLOY_ROOT}}/releases" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')" -ge 2
    grep -q '不是 git 仓库；使用当前发布物快照继续' "${{LOG_DIR}}/update.err"
    grep -q 'phase=migrate_db status=done' "${{LOG_DIR}}/update.out"
    grep -q 'phase=restart_services status=done' "${{LOG_DIR}}/update.out"
    grep -q 'phase=health_check status=done' "${{LOG_DIR}}/update.out"

    LUMEN_UNINSTALL_NONINTERACTIVE=1 bash "${{DEPLOY_ROOT}}/current/scripts/lumenctl.sh" uninstall-lumen > "${{LOG_DIR}}/uninstall.out" 2> "${{LOG_DIR}}/uninstall.err"
    grep -q '卸载总结' "${{LOG_DIR}}/uninstall.out"
    grep -q '已 docker compose down 主栈' "${{LOG_DIR}}/uninstall.out"
    """

    result = run_bash(script)

    def _maybe_log(name: str) -> str:
        path = log_dir / name
        if not path.exists():
            return f"\n{name}: <missing>\n"
        return f"\n{name}:\n" + path.read_text(encoding="utf-8", errors="replace")

    assert result.returncode == 0, (
        result.stderr
        + result.stdout
        + _maybe_log("install.out")
        + _maybe_log("install.err")
        + _maybe_log("update.out")
        + _maybe_log("update.err")
        + _maybe_log("uninstall.out")
        + _maybe_log("uninstall.err")
        + _maybe_log("docker.log")
    )


def test_lumenctl_validators_reject_unsafe_values() -> None:
    assert_bash_ok(
        f"""
        . {LUMENCTL}
        validate_domain_list domain 'example.com *.example.com _'
        ! validate_domain_list domain 'example.com;'
        ! validate_domain_list domain 'bad$name'
        ! validate_domain_list domain '*'
        validate_tcp_port port 1
        validate_tcp_port port 65535
        ! validate_tcp_port port 0
        ! validate_tcp_port port 65536
        validate_url_like url http://127.0.0.1:8081
        ! validate_url_like url ftp://127.0.0.1
        validate_absolute_path path /opt/image-job
        ! validate_absolute_path path relative/path
        validate_service_user_name user image-job
        validate_service_user_name user root
        ! validate_service_user_name user 'bad user'
        validate_python_command py python3
        validate_python_command py /usr/bin/python3
        ! validate_python_command py ../python3
        """
    )


def test_probe_sub2api_upstream_accepts_openai_compatible_http_status() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        ensure_cmd() {{ :; }}
        curl() {{
          printf '401'
        }}
        probe_sub2api_upstream http://10.0.0.8:18080
        """
    )
    assert "http://10.0.0.8:18080/v1/models" in result.stdout
    assert "sub2api/OpenAI 兼容端点探测通过" in result.stdout


def test_probe_sub2api_upstream_uses_health_as_reachability_fallback() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        ensure_cmd() {{ :; }}
        curl() {{
          case "$*" in
            *'/health'*) printf '200' ;;
            *) printf '000' ;;
          esac
        }}
        probe_sub2api_upstream https://sub2api.example.com
        """
    )
    assert "https://sub2api.example.com/health" in result.stderr
    assert "请确认它是 sub2api/OpenAI 兼容服务" in result.stderr


def test_probe_sub2api_upstream_fails_when_unreachable() -> None:
    result = run_bash(
        f"""
        . {LUMENCTL}
        ensure_cmd() {{ :; }}
        curl() {{
          printf '000'
        }}
        probe_sub2api_upstream http://127.0.0.1:19091
        """
    )
    assert result.returncode == 1
    assert "无法连接 sub2api/OpenAI 兼容上游" in result.stderr


def test_lumen_nginx_config_contains_sse_api_and_security_defaults(tmp_path: Path) -> None:
    out = tmp_path / "lumen.conf"
    assert_bash_ok(
        f"""
        . {LUMENCTL}
        write_lumen_nginx_config {out} 'lumen.example.com www.example.com' '127.0.0.1:3000' 1 1 /etc/ssl/fullchain.pem /etc/ssl/privkey.pem
        """
    )
    config = out.read_text(encoding="utf-8")
    assert "return 301 https://$host$request_uri;" in config
    assert "location /events" in config
    assert "proxy_buffering off;" in config
    assert "location /api/" in config
    assert "limit_req_zone $binary_remote_addr zone=lumen_api_lumen_example_com" in config
    assert "add_header X-Content-Type-Options" in config
    assert "client_max_body_size 60m;" in config


def test_sub2api_nginx_configs_have_long_timeouts_and_buffering_off(tmp_path: Path) -> None:
    sub2api = tmp_path / "sub2api.conf"
    outer = tmp_path / "outer.conf"
    assert_bash_ok(
        f"""
        . {LUMENCTL}
        write_sub2api_nginx_config {sub2api} api.example.com http://127.0.0.1:8081 1 /etc/ssl/fullchain.pem /etc/ssl/privkey.pem 443 1
        write_sub2api_outer_nginx_config {outer} api.example.com http://10.0.0.2:18081 0 '' ''
        """
    )
    for path in (sub2api, outer):
        config = path.read_text(encoding="utf-8")
        assert "client_max_body_size 100M;" in config
        assert "proxy_send_timeout 1800s;" in config
        assert "proxy_read_timeout 1800s;" in config
        assert "proxy_buffering off;" in config
        assert "proxy_request_buffering off;" in config


def test_nginx_backup_path_is_outside_enabled_config_tree(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        LUMEN_NGINX_BACKUP_DIR={backup_dir}
        nginx_backup_path /etc/nginx/sites-enabled/lumen.conf 20260502010101
        """
    )
    backup_path = Path(result.stdout.strip())
    assert backup_path.parent == backup_dir
    assert backup_path.name.endswith(".lumenctl.20260502010101.bak")
    assert "sites-enabled" in backup_path.name
    assert not backup_path.is_relative_to(Path("/etc/nginx"))


def test_nginx_image_job_returns_cleanly_when_scan_finds_no_files() -> None:
    result = assert_bash_ok(
        f"""
        . {LUMENCTL}
        require_sudo() {{ :; }}
        ensure_cmd() {{ :; }}
        as_sudo() {{
          if [ "$1" = "nginx" ] && [ "$2" = "-t" ]; then
            return 0
          fi
          "$@"
        }}
        collect_nginx_files() {{ NGINX_FILES=(); }}
        nginx_image_job_locations
        """
    )
    assert "没有可优化的 nginx 配置文件" in result.stderr


def test_image_job_location_injection_is_scoped_and_idempotent(tmp_path: Path) -> None:
    nginx_conf = tmp_path / "site.conf"
    nginx_conf.write_text(
        """
server {
  listen 80;
  server_name example.com;
  location / { proxy_pass http://127.0.0.1:3000; }
}

server {
  listen 443 ssl;
  server_name example.com;
  location / { proxy_pass http://127.0.0.1:3000; }
}
""",
        encoding="utf-8",
    )
    tmp1 = assert_bash_ok(
        f"""
        . {LUMENCTL}
        optimize_nginx_file {nginx_conf} http://127.0.0.1:8091 /opt/image-job/data example.com
        """
    ).stdout.strip().splitlines()[-1]
    first = Path(tmp1).read_text(encoding="utf-8")
    nginx_conf.write_text(first, encoding="utf-8")
    tmp2 = assert_bash_ok(
        f"""
        . {LUMENCTL}
        optimize_nginx_file {nginx_conf} http://127.0.0.1:8091 /opt/image-job/data example.com
        """
    ).stdout.strip().splitlines()[-1]
    second = Path(tmp2).read_text(encoding="utf-8")

    assert first == second
    assert first.count("location ^~ /v1/image-jobs") == 1
    assert first.count("location ^~ /v1/refs") == 1
    assert first.count("location ^~ /images/temp/") == 1
    assert first.count("location ^~ /refs/") == 1
    http_block, https_block = first.split("server {", 2)[1:]
    assert "/v1/image-jobs" not in http_block
    assert "/v1/image-jobs" in https_block


# ---------------------------------------------------------------------------
# Docker 全栈切换：脚本互相之间的契约（install / update / uninstall + lib helper）
# ---------------------------------------------------------------------------


def test_install_script_uses_lumen_compose_helpers_from_lib() -> None:
    """
    docker cutover §6.2 / §10.2: install.sh 走 lib.sh 提供的 lumen_compose helper
    （或本地 _install_compose 包装），不再 systemctl restart lumen-*。
    """
    text = INSTALL.read_text(encoding="utf-8")
    # 显式引用 lib.sh 的 compose helper（lumen_compose 或 lumen_compose_in）
    assert "lumen_compose" in text
    # 流程描述里出现 docker compose pull / migrate / api/worker/web
    assert "docker compose pull" in text
    assert "migrate" in text
    # set -euo pipefail 必须保留
    assert "set -euo pipefail" in text
    # source lib.sh
    assert ". \"${SCRIPT_DIR}/lib.sh\"" in text


def test_update_script_emits_set_image_tag_and_migrate_db_phases() -> None:
    """
    docker cutover §11.3.1: update.sh 阶段日志里必须包含 set_image_tag 与 migrate_db，
    后台一键更新解析这两个阶段决定 LUMEN_IMAGE_TAG 是否切换 / 数据库迁移是否成功。
    """
    text = UPDATE.read_text(encoding="utf-8")
    assert "set_image_tag" in text
    assert "migrate_db" in text
    # phase=set_image_tag 与 phase=migrate_db 在最终输出里靠 emit_start 拼出，
    # emit_start 的展开 = lumen_emit_step "phase=$1" "status=start"
    assert 'lumen_emit_step "phase=$1"' in text
    assert "emit_start set_image_tag" in text
    assert "emit_start migrate_db" in text
    # 必须 source lib.sh 并 set -euo pipefail
    assert "set -euo pipefail" in text
    assert "lib.sh" in text


def test_update_script_runner_default_pull_not_build() -> None:
    """
    docker cutover §11.3.2 + §12.3.2: build 必须由 LUMEN_UPDATE_BUILD=1 显式开启，
    runner systemd 默认 LUMEN_UPDATE_BUILD=0（pull 优先）。
    """
    text = UPDATE.read_text(encoding="utf-8")
    # build 路径必须 gated by env
    assert 'LUMEN_UPDATE_BUILD:-0' in text
    runner_unit = (
        ROOT / "deploy" / "systemd" / "lumen-update-runner.service"
    ).read_text(encoding="utf-8")
    assert "LUMEN_UPDATE_BUILD=0" in runner_unit


def test_uninstall_script_uses_docker_compose_down() -> None:
    """
    docker cutover §17.4 / §17.9: uninstall.sh 走 docker compose down --remove-orphans，
    不再依赖 systemctl stop lumen-* 作为停服主路径。
    """
    text = UNINSTALL.read_text(encoding="utf-8")
    # 主路径必须有 docker compose down
    assert "docker compose down" in text
    # 优先走 lib.sh 的 lumen_compose_in（带 COMPOSE_PROJECT_NAME=lumen）
    assert "lumen_compose_in" in text
    # 含 --profile tgbot 的 down，确保 profile 服务也清理
    assert "--profile tgbot" in text
    # set -euo pipefail + source lib.sh 必须保留
    assert "set -euo pipefail" in text
    assert "lib.sh" in text


def test_lib_provides_compose_helpers_required_by_cutover() -> None:
    """
    docker cutover: lib.sh 必须暴露 cutover plan §3.1 / §11 / §13 列出的全部 helper。
    """
    text = LIB.read_text(encoding="utf-8")
    for fn in (
        "lumen_compose()",
        "lumen_compose_in()",
        "lumen_health_http()",
        "lumen_health_compose()",
        "lumen_image_tag_resolve()",
        "lumen_set_image_tag_in_env()",
        "lumen_emit_step()",
        "lumen_emit_info()",
        "lumen_with_lock()",
    ):
        assert fn in text, f"lib.sh 缺少 helper：{fn}"


def test_image_tag_resolve_uses_channel_and_env_file_for_pinned(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LUMEN_IMAGE_TAG=v1.2.3\n", encoding="utf-8")

    result = assert_bash_ok(
        f"""
        . {LIB}
        lumen_image_tag_resolve pinned {env_file}
        """
    )

    assert result.stdout.strip() == "v1.2.3"


def test_docker_release_workflow_builds_amd64_and_arm64() -> None:
    workflow = (ROOT / ".github" / "workflows" / "docker-release.yml").read_text(
        encoding="utf-8"
    )
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "needs: quality-gate" in workflow
    assert "type=semver,pattern=v{{version}}" in workflow
    assert "type=semver,pattern=v{{major}}.{{minor}}" in workflow
    assert "type=semver,pattern=v{{major}}" in workflow
    assert "cp .env.example .env" in workflow
    assert "Compose config" in workflow
    assert "Image start smoke" in workflow


def test_deploy_docker_helper_files_exist() -> None:
    assert (ROOT / "deploy" / "docker" / "README.md").is_file()
    override = (ROOT / "deploy" / "docker" / "docker-compose.local.yml").read_text(
        encoding="utf-8"
    )
    assert "lumen-local-api" in override
    assert "18000:8000" in override
    assert "13000:3000" in override


def test_admin_update_checklist_uses_docker_phases() -> None:
    panel = (
        ROOT / "apps" / "web" / "src" / "app" / "admin" / "_panels" / "SettingsPanel.tsx"
    ).read_text(encoding="utf-8")
    for phase in (
        "lock",
        "check",
        "preflight",
        "backup_preflight",
        "fetch_release",
        "set_image_tag",
        "pull_images",
        "start_infra",
        "migrate_db",
        "switch",
        "restart_services",
        "health_check",
        "cleanup",
    ):
        assert f'"{phase}"' in panel
    for old_phase in ("deps_python", "deps_node", "build_web"):
        assert f'"{old_phase}"' not in panel


def test_lumenctl_help_documents_docker_compose_runtime_block() -> None:
    """
    docker cutover §24: lumenctl.sh help 输出里必须出现 status / logs / migrate / rollback / version
    这些 docker compose 阶段命令的描述（菜单 + CLI 双入口）。
    """
    result = subprocess.run(
        ["bash", str(LUMENCTL), "help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    output = result.stdout
    # help 描述里出现 docker compose 关键字（确认 help 在描述中提到 compose 路径）
    assert "docker compose" in output or "compose" in output
    # 关键 lifecycle / runtime 命令名在描述行里
    for keyword in ("rollback", "migrate", "version", "status", "logs"):
        assert keyword in output, f"lumenctl.sh help 描述里缺少 {keyword}"
