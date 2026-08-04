from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
WORKFLOWS = ROOT / ".github" / "workflows"
COMPOSE = ROOT / "docker-compose.yml"
BLUEGREEN_COMPOSE = ROOT / "docker-compose.bluegreen.yml"
ALEMBIC_VERSIONS = ROOT / "apps" / "api" / "alembic" / "versions"
STORAGE_MOUNT = ROOT / "deploy" / "scripts" / "lumen_storage_mount.sh"
FIX_REDIS_PASSWORD = ROOT / "scripts" / "fix-redis-password-mismatch.sh"
SHIFT_TRAFFIC = ROOT / "scripts" / "lumen-shift-traffic.sh"


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


def _module_string_assignment(path: Path, name: str) -> str | None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        value: ast.AST | None = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == name:
                value = node.value
        elif isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            ):
                value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def test_alembic_revision_ids_fit_default_version_column() -> None:
    too_long: list[str] = []
    for path in sorted(ALEMBIC_VERSIONS.glob("*.py")):
        revision = _module_string_assignment(path, "revision")
        if revision is None:
            continue
        if len(revision) > 32:
            too_long.append(f"{path.relative_to(ROOT)}: {revision}")

    assert too_long == []


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
    public_dns_compose = (ROOT / "docker-compose.public-dns.yml").read_text(
        encoding="utf-8"
    )
    bluegreen = BLUEGREEN_COMPOSE.read_text(encoding="utf-8")

    assert "ulimits:" in compose
    assert "LUMEN_ULIMIT_NOFILE_SOFT" in compose
    assert "LUMEN_ULIMIT_NPROC" in compose
    assert "http://127.0.0.1:3000/api/healthz" not in compose
    web = re.search(r"(?ms)^  web:\n(?P<body>.*?)(?=^  \w|\Z)", compose)
    assert web is not None
    assert "wget" not in web.group("body")
    assert "path: '/healthz', method: 'HEAD'" in compose
    assert 'command: ["python", "-m", "app.worker_health", "run"]' in compose
    assert (
        'LUMEN_WORKER_HEALTH_KEY_PREFIX: "${LUMEN_WORKER_HEALTH_KEY_PREFIX:-arq:queue:health-check}"'
        in compose
    )
    assert 'test: ["CMD", "python", "-m", "app.worker_health", "check"]' in compose
    assert "LUMEN_WORKER_HEALTH_KEY:" not in compose
    assert 'test: ["CMD", "python", "-m", "app.tgbot_health", "check"]' in compose
    assert "TGBOT_HEALTH_READY_STABILITY_SECONDS:" in compose
    assert '"${LUMEN_WORKER_DNS_PRIMARY:-1.1.1.1}"' not in compose
    assert '"${LUMEN_WORKER_DNS_SECONDARY:-8.8.8.8}"' not in compose
    assert '"${LUMEN_WORKER_DNS_PRIMARY:-1.1.1.1}"' in public_dns_compose
    assert '"${LUMEN_WORKER_DNS_SECONDARY:-8.8.8.8}"' in public_dns_compose

    assert "api-green:" in bluegreen
    assert "init: true" in bluegreen
    assert "LUMEN_ULIMIT_NOFILE_SOFT" in bluegreen


def test_api_worker_count_is_shared_by_uvicorn_and_capacity_scaling() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    api = compose["services"]["api"]

    worker_flag = api["command"].index("--workers")
    assert api["command"][worker_flag + 1] == "${LUMEN_API_WORKERS:-2}"
    assert api["environment"]["LUMEN_API_WORKERS"] == "${LUMEN_API_WORKERS:-2}"


def test_compose_one_shot_profiles_do_not_auto_restart() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")

    for service in ("migrate", "bootstrap"):
        match = re.search(rf"(?ms)^  {service}:\n(?P<body>.*?)(?=^  \w|\Z)", compose)
        assert match is not None, f"{service} service missing"
        assert 'restart: "no"' in match.group("body")
        assert "on-failure" not in match.group("body")


def test_compose_tgbot_starts_python_before_validating_runtime_secret() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    tgbot = compose["services"]["tgbot"]

    assert tgbot["command"] == ["python", "-m", "app.main"]
    assert tgbot["restart"] == "unless-stopped"
    assert (
        tgbot["environment"]["TELEGRAM_BOT_SHARED_SECRET"]
        == "${TELEGRAM_BOT_SHARED_SECRET:-}"
    )


def test_compose_telegram_ssh_proxy_is_reachable_only_on_backend_network() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    api = compose["services"]["api"]
    tgbot = compose["services"]["tgbot"]

    assert (
        api["environment"]["TELEGRAM_PROXY_BIND_HOST"]
        == "${TELEGRAM_PROXY_BIND_HOST:-0.0.0.0}"
    )
    assert (
        api["environment"]["TELEGRAM_PROXY_ADVERTISE_HOST"]
        == "${TELEGRAM_PROXY_ADVERTISE_HOST:-api}"
    )
    assert api["networks"] == ["lumen_backend"]
    assert tgbot["networks"] == ["lumen_backend"]
    assert tgbot["environment"]["LUMEN_API_BASE"] == "http://api:8000"
    # Only the HTTP API port is host-published. The SSH SOCKS port is allocated
    # dynamically inside the API container and remains reachable by service DNS.
    assert api["ports"] == [
        "${API_BIND_HOST:-127.0.0.1}:${API_BIND_PORT:-8000}:8000"
    ]


def test_storage_mount_cleans_smb_credentials_on_hard_failures() -> None:
    text = STORAGE_MOUNT.read_text(encoding="utf-8")

    assert text.count("trap \"rm -f '$cred'\" RETURN EXIT") == 2
    assert "if mount -t cifs" in text
    assert 'rm -f "$cred"' in text
    assert "trap - EXIT" in text


def test_storage_mount_config_parser_does_not_execute_conf_shell(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    target = tmp_path / "target"
    pwned = tmp_path / "pwned"
    state_dir.mkdir()
    target.mkdir()
    (state_dir / "storage.conf").write_text(
        f"MODE=$(touch {shlex.quote(str(pwned))})\n"
        "LOCAL_ROOT='/tmp/lumen local root'\n"
        "SMB_PASSWORD='$(id >/tmp/lumen-owned)'\n",
        encoding="utf-8",
    )

    result = _run_bash(
        f"""
        LUMEN_STORAGE_STATE_DIR={shlex.quote(str(state_dir))} \
        LUMEN_STORAGE_TARGET={shlex.quote(str(target))} \
          bash {shlex.quote(str(STORAGE_MOUNT))} status >/dev/null
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert not pwned.exists()
    text = STORAGE_MOUNT.read_text(encoding="utf-8")
    assert '. "$CONF_FILE"' not in text
    assert '. "$TEST_CONF_FILE"' not in text


def test_privileged_trigger_services_do_not_load_api_writable_environment_files() -> (
    None
):
    root = Path(__file__).resolve().parents[1]
    units = (
        root / "deploy/systemd/lumen-update-runner.service",
        root / "deploy/systemd/lumen-update-warm.service",
        root / "deploy/systemd/lumen-storage-apply.service",
        root / "deploy/systemd/lumen-storage-test.service",
    )

    for unit in units:
        text = unit.read_text(encoding="utf-8")
        assert "EnvironmentFile=-/opt/lumendata/backup/.update.env" not in text
        assert "EnvironmentFile=-/var/lib/lumen-storage/apply.env" not in text
        assert "EnvironmentFile=-/var/lib/lumen-storage/test.env" not in text


def test_service_units_do_not_cross_privilege_writable_runtime_state() -> None:
    root = Path(__file__).resolve().parents[1]
    api = (root / "deploy/systemd/lumen-api.service").read_text(encoding="utf-8")
    worker = (root / "deploy/systemd/lumen-worker.service").read_text(
        encoding="utf-8"
    )
    backup = (root / "deploy/systemd/lumen-backup.service").read_text(
        encoding="utf-8"
    )
    restore = (root / "deploy/systemd/lumen-restore-runner.service").read_text(
        encoding="utf-8"
    )

    for unit in (api, worker, backup):
        assert "ExecStartPre=+" not in unit
    assert "ReadWritePaths=/opt/lumendata /opt/lumen/shared" not in api
    assert "ReadWritePaths=/opt/lumendata /opt/lumen/shared" not in worker
    assert "/opt/lumen/shared/worker-var" in worker
    assert (
        "Environment=LUMEN_WORKER_HEALTH_STATE_FILE="
        "/opt/lumen/shared/worker-var/worker-health.json"
    ) in worker
    assert (
        "ExecStart=/opt/lumen/current/.venv/bin/python "
        "-m app.worker_health run"
    ) in worker
    assert "EnvironmentFile=" not in backup
    assert "EnvironmentFile=" not in restore
    assert "Environment=LUMEN_ENV_FILE=/opt/lumen/shared/.env" in backup
    assert "Environment=LUMEN_ENV_FILE=/opt/lumen/shared/.env" in restore


def test_release_migration_does_not_delegate_code_or_secrets_to_service_user() -> (
    None
):
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts/migrate_to_releases.sh").read_text(encoding="utf-8")
    runtime = (root / "scripts/lib/runtime.sh").read_text(encoding="utf-8")

    assert 'chown -R lumen:lumen "${ROOT}/releases" "${ROOT}/shared"' not in source
    assert "lumen_release_harden_ownership" in source
    assert 'config_group="${LUMEN_BACKUP_SERVICE_GROUP:-lumen-backup}"' in source
    assert 'chgrp "${config_group}" "${shared_env}"' in runtime
    assert 'usermod -aG "${config_group}" lumen' in runtime
    release_layout = (root / "scripts/lib/release_layout.sh").read_text(
        encoding="utf-8"
    )
    assert 'chown "root:${config_group}" "${shared_dir}/.env"' in release_layout
    assert 'chmod 0640 "${shared_dir}/.env"' in release_layout
    assert 'chmod 0600 "${shared_dir}/.env"' not in release_layout


def test_direct_nonroot_install_keeps_env_readable_via_existing_operator_group(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    release = tmp_path / "releases" / "new"
    shared.mkdir()
    release.mkdir(parents=True)
    env_file = shared / ".env"
    env_file.write_text("SECRET=value\n", encoding="utf-8")
    operator_group = subprocess.run(
        ["id", "-gn"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    captured_group = tmp_path / "group"
    operations = ROOT / "scripts" / "install" / "operations.sh"

    result = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(operations))}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        log_info() {{ :; }}
        lumen_ensure_backup_service_user() {{ :; }}
        lumen_release_harden_ownership() {{
          printf '%s\\n' "$6" > {shlex.quote(str(captured_group))}
          chmod 0640 {shlex.quote(str(env_file))}
        }}
        install_transaction_harden_journal() {{ :; }}
        DEPLOY_ROOT={shlex.quote(str(tmp_path))}
        RELEASE_DIR={shlex.quote(str(release))}
        SHARED_DIR={shlex.quote(str(shared))}
        LUMEN_DATA_ROOT={shlex.quote(str(tmp_path / "data"))}
        LUMEN_APP_UID="$(id -u)"
        LUMEN_APP_GID="$(id -g)"
        LUMEN_INSTALL_CONFIG_GROUP={shlex.quote(operator_group)}
        harden_install_release_ownership
        test -r {shlex.quote(str(env_file))}
        test "$(stat -f '%Lp' {shlex.quote(str(env_file))} 2>/dev/null \
          || stat -c '%a' {shlex.quote(str(env_file))})" = 640
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert captured_group.read_text(encoding="utf-8").strip() == operator_group
    assert env_file.stat().st_mode & 0o007 == 0


def test_direct_install_rejects_config_group_operator_does_not_have(
    tmp_path: Path,
) -> None:
    operations = ROOT / "scripts" / "install" / "operations.sh"
    result = _run_bash(
        f"""
        set -euo pipefail
        . {shlex.quote(str(operations))}
        log_error() {{ printf 'ERROR:%s\\n' "$*" >&2; }}
        LUMEN_INSTALL_OPERATOR_USER="$(id -un)"
        LUMEN_INSTALL_CONFIG_GROUP=lumen-no-such-operator-group
        if install_config_read_group; then
          exit 91
        fi
        """
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert "当前不属于配置读取组" in result.stderr


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
