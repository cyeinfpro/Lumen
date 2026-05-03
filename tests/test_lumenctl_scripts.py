from __future__ import annotations

import os
import subprocess
from pathlib import Path


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


def test_install_script_defaults_to_starting_runtime_after_install() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "LUMEN_AUTO_START_RUNTIME:-1" in text
    assert "START_RUNTIME_REPLY=\"$(read_or_default '现在启动 API / Worker / Web" in text
    assert "start_runtime_processes \"${WEB_NPM_SCRIPT}\"" in text
    assert "lumen_start_local_runtime \"${ROOT}\" \"${web_npm_script}\"" in text
    assert "运行状态 ......... 已启动 API / Worker / Web" in text
    assert "未启动前，浏览器访问 3000 不会有响应" in text


def test_update_script_requires_release_layout_and_prepares_new_release() -> None:
    text = (ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
    assert "Capistrano 风格 release + symlink 原子切换版" in text
    assert 'if [ ! -L "${ROOT}/current" ]; then' in text
    assert "migrate_to_releases.sh" in text
    assert 'NEW_RELEASE="${ROOT}/releases/${NEW_ID}"' in text
    assert 'lumen_release_id "${PREP_SHA}"' in text
    assert 'GIT_BIN}" clone --quiet' in text
    assert '"${GIT_REMOTE_URL}" "${NEW_RELEASE}"' in text
    assert 'cat > "${NEW_RELEASE}/.lumen_release.json" <<JSON' in text
    assert 'lumen_release_link_shared "${NEW_RELEASE}" "${ROOT}/shared"' in text


def test_update_script_restarts_services_and_health_checks_after_update() -> None:
    text = UPDATE.read_text(encoding="utf-8")
    assert 'lumen_step_begin switch' in text
    assert 'lumen_release_atomic_switch "${ROOT}" "${NEW_ID}"' in text
    assert 'lumen_step_begin restart' in text
    assert 'log_step "[restart] 重启 systemd 服务"' in text
    assert 'lumen_ensure_runtime_dirs "${ROOT}/.env"' in text
    assert (
        "for _LUMEN_UNIT in lumen-worker.service lumen-web.service "
        "lumen-tgbot.service lumen-api.service; do"
    ) in text
    assert 'LUMEN_RESTART_UNITS+=("${_LUMEN_UNIT}")' in text
    assert 'lumen_restart_systemd_units "${LUMEN_RESTART_UNITS[@]}"' in text
    assert 'lumen_step_begin health_post' in text
    assert "lumen_check_runtime_health" in text
    assert 'lumen_step_begin cleanup' in text
    assert 'lumen_release_cleanup_old "${ROOT}" "${LUMEN_RELEASE_KEEP:-5}"' in text
    assert 'log_info "release ${NEW_ID} 已上线（previous: ${CURRENT_ID}）"' in text


def test_update_script_runs_dependency_steps_as_systemd_runtime_user() -> None:
    text = UPDATE.read_text(encoding="utf-8")
    assert "LUMEN_UPDATE_SYSTEMD_RUNTIME=1" in text
    assert 'LUMEN_UPDATE_RUN_USER="$(lumen_runtime_service_user)"' in text
    assert 'lumen_update_as_runtime_user "${UV_BIN}" sync --frozen --all-packages' in text
    assert 'lumen_update_as_runtime_user "${UV_BIN}" run alembic upgrade head' in text
    assert 'lumen_update_as_runtime_user "${NPM_BIN}" ci' in text
    assert 'lumen_update_as_runtime_user "${NPM_BIN}" run build' in text


def test_shared_runtime_health_helpers_cover_api_web_worker() -> None:
    text = LIB.read_text(encoding="utf-8")
    assert "lumen_check_runtime_health()" in text
    assert "http://127.0.0.1:8000/healthz" in text
    assert "http://127.0.0.1:3000/" in text
    assert "lumen_systemd_unit_active lumen-worker.service" in text
    assert "lumen_start_local_runtime()" in text
    assert "lumen_tail_runtime_log \"Worker\"" in text


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
    ):
        assert f"  {command}" in output


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
        run_lumen_script() {{ printf 'run_lumen_script:%s\\n' "$1"; }}
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
        "run_lumen_script:install.sh",
        "run_lumen_script:update.sh",
        "run_lumen_script:uninstall.sh",
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
        run_lumen_script() {{ printf 'run_lumen_script:%s\\n' "$1"; }}
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
        "run_lumen_script:install.sh",
        "run_lumen_script:update.sh",
        "run_lumen_script:uninstall.sh",
        "install_image_job",
        "uninstall_image_job",
        "nginx_scan",
        "nginx_optimize",
    ]


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
