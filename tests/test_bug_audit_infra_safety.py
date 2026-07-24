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
            if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
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
    assert "health_check_interval=int(os.getenv" in compose
    assert (
        'LUMEN_WORKER_HEALTH_KEY: "${LUMEN_WORKER_HEALTH_KEY:-arq:queue:health-check}"'
        in compose
    )
    assert "redis_client.get(key)" in compose
    assert 'needle = b"\\x00-m\\x00app.main\\x00"' in compose
    assert 'redis.from_url(os.environ["REDIS_URL"]).ping()' in compose
    assert '"${LUMEN_WORKER_DNS_PRIMARY:-1.1.1.1}"' not in compose
    assert '"${LUMEN_WORKER_DNS_SECONDARY:-8.8.8.8}"' not in compose
    assert '"${LUMEN_WORKER_DNS_PRIMARY:-1.1.1.1}"' in public_dns_compose
    assert '"${LUMEN_WORKER_DNS_SECONDARY:-8.8.8.8}"' in public_dns_compose

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


def test_compose_tgbot_starts_python_before_validating_runtime_secret() -> None:
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    tgbot = compose["services"]["tgbot"]

    assert tgbot["command"] == ["python", "-m", "app.main"]
    assert tgbot["restart"] == "unless-stopped"
    assert (
        tgbot["environment"]["TELEGRAM_BOT_SHARED_SECRET"]
        == "${TELEGRAM_BOT_SHARED_SECRET:-}"
    )


def test_storage_mount_cleans_smb_credentials_on_hard_failures() -> None:
    text = STORAGE_MOUNT.read_text(encoding="utf-8")

    assert text.count("trap \"rm -f '$cred'\" RETURN EXIT") == 2
    assert "if mount -t cifs" in text
    assert 'rm -f "$cred"' in text
    assert "trap - EXIT" in text


def test_storage_mount_config_parser_does_not_execute_conf_shell(tmp_path: Path) -> (
    None
):
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
