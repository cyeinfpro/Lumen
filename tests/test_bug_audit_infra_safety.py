from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts" / "lib.sh"
WORKFLOWS = ROOT / ".github" / "workflows"
COMPOSE = ROOT / "docker-compose.yml"
BLUEGREEN_COMPOSE = ROOT / "docker-compose.bluegreen.yml"
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
    bluegreen = BLUEGREEN_COMPOSE.read_text(encoding="utf-8")

    assert "ulimits:" in compose
    assert "LUMEN_ULIMIT_NOFILE_SOFT" in compose
    assert "LUMEN_ULIMIT_NPROC" in compose
    assert "http://127.0.0.1:3000/api/healthz" not in compose
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
