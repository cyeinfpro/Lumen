#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
DMG="${1:-$ROOT/apps/desktop/target/release/bundle/dmg/Lumen_$(cat "$ROOT/VERSION")_aarch64.dmg}"

if [ ! -f "$DMG" ]; then
  echo "missing dmg: $DMG" >&2
  exit 1
fi

work="$(mktemp -d)"
mount="$work/mnt"
home="$work/home"
mkdir -p "$mount" "$home"
app_pid=""

cleanup() {
  set +e
  if [ -n "$app_pid" ]; then
    pkill -P "$app_pid" >/dev/null 2>&1 || true
    kill "$app_pid" >/dev/null 2>&1 || true
    sleep 1
    pkill -P "$app_pid" >/dev/null 2>&1 || true
    kill -9 "$app_pid" >/dev/null 2>&1 || true
    wait "$app_pid" >/dev/null 2>&1 || true
  fi
  pkill -f "$mount/Lumen.app/Contents" >/dev/null 2>&1 || true
  hdiutil detach "$mount" -quiet >/dev/null 2>&1 \
    || hdiutil detach "$mount" -force -quiet >/dev/null 2>&1 \
    || true
  if ! mount | grep -F "$mount" >/dev/null 2>&1; then
    rm -rf "$work"
  else
    echo "cleanup_kept_mount=$mount" >&2
  fi
}
trap cleanup EXIT

hdiutil attach "$DMG" -nobrowse -readonly -mountpoint "$mount" -quiet
app="$mount/Lumen.app"
if [ ! -d "$app" ]; then
  echo "missing Lumen.app in dmg" >&2
  find "$mount" -maxdepth 2 -print >&2
  exit 1
fi

echo "mounted_app=$app"
echo "bundle_executable=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$app/Contents/Info.plist")"
find "$app/Contents" -maxdepth 5 \( -name 'lumen-*' -o -name 'dotnet' -o -name 'node' -o -name 'libsqlite_vec*' \) -print | sort

if strings "$app/Contents/MacOS/lumen-desktop" | grep -- '--logdir' >/dev/null; then
  echo "old Garnet --logdir argument is still present" >&2
  exit 1
fi

(
  unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy NO_PROXY no_proxy
  export HOME="$home"
  export APPLE_DISABLE_SANDBOX=1
  "$app/Contents/MacOS/lumen-desktop"
) >"$work/app.stdout.log" 2>"$work/app.stderr.log" &
app_pid=$!
disown "$app_pid" 2>/dev/null || true

HOME_DIR="$home" WORK_DIR="$work" MOUNT_DIR="$mount" APP_PID="$app_pid" python3 - <<'PY'
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
import urllib.request

home = pathlib.Path(os.environ["HOME_DIR"])
work = pathlib.Path(os.environ["WORK_DIR"])
mount = os.environ["MOUNT_DIR"]
app_pid = int(os.environ["APP_PID"])
mount_markers = {mount, os.path.realpath(mount)}
logs_root = home / "Library/Application Support/com.lumen.desktop/data/logs"
api_port = None
web_port = None


def read_log(name: str) -> str:
    path = logs_root / name
    return path.read_text(errors="replace") if path.exists() else ""


def process_alive(pid: int) -> bool:
    return (
        subprocess.run(
            ["kill", "-0", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def get_http(port: int, path: str) -> int:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"Connection": "close"}
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        response.read(512)
        return response.status


def listening_pids(port: int) -> list[int]:
    try:
        output = subprocess.check_output(
            ["lsof", "-nP", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            text=True,
            errors="replace",
        )
    except subprocess.CalledProcessError:
        return []
    pids: list[int] = []
    for line in output.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            pass
    return pids


def sidecar_pids(name: str) -> list[int]:
    if name == "lumen-web" and web_port is not None:
        pids = listening_pids(web_port)
        if pids:
            return pids
    ps_output = subprocess.check_output(
        ["ps", "ax", "-o", "pid,ppid,command"], text=True, errors="replace"
    )
    pids: list[int] = []
    for line in ps_output.splitlines():
        if not any(marker in line for marker in mount_markers):
            continue
        if name == "lumen-web":
            matched = "lumen-web" in line or "server.js" in line
        else:
            matched = name in line
        if not matched:
            continue
        parts = line.strip().split(None, 2)
        if not parts:
            continue
        try:
            pids.append(int(parts[0]))
        except ValueError:
            pass
    return pids


def wait_until_ready(seconds: float) -> bool:
    global api_port, web_port
    deadline = time.time() + seconds
    while time.time() < deadline:
        api_err = read_log("api.err.log")
        web_log = read_log("web.log")
        match = re.search(r"Uvicorn running on http://127\.0\.0\.1:(\d+)", api_err)
        if match:
            api_port = int(match.group(1))
        match = re.search(r"Local:\s+http://localhost:(\d+)", web_log)
        if match:
            web_port = int(match.group(1))
        if api_port and web_port:
            try:
                if get_http(api_port, "/system/desktop-ready") == 200 and get_http(web_port, "/") == 200:
                    return True
            except Exception:
                pass
        if not process_alive(app_pid):
            break
        time.sleep(0.25)
    return False

baseline_ready = wait_until_ready(60)
worker_restarted = False
worker_before = set(sidecar_pids("lumen-worker"))
if worker_before:
    os.kill(next(iter(worker_before)), signal.SIGTERM)
    restart_deadline = time.time() + 15
    while time.time() < restart_deadline:
        worker_after = set(sidecar_pids("lumen-worker"))
        if worker_after and not worker_after.issubset(worker_before):
            worker_restarted = True
            break
        if not process_alive(app_pid):
            break
        time.sleep(0.25)

web_restarted = False
web_before = set(sidecar_pids("lumen-web"))
if web_before:
    os.kill(next(iter(web_before)), signal.SIGTERM)
    restart_deadline = time.time() + 20
    while time.time() < restart_deadline:
        web_after = set(sidecar_pids("lumen-web"))
        try:
            web_ok = web_port is not None and get_http(web_port, "/") == 200
        except Exception:
            web_ok = False
        if web_after and not web_after.issubset(web_before) and web_ok:
            web_restarted = True
            break
        if not process_alive(app_pid):
            break
        time.sleep(0.25)

api_restarted = False
api_before = set(sidecar_pids("lumen-api"))
if api_before:
    os.kill(next(iter(api_before)), signal.SIGTERM)
    restart_deadline = time.time() + 45
    while time.time() < restart_deadline:
        api_after = set(sidecar_pids("lumen-api"))
        all_sidecars_alive = all(
            sidecar_pids(name)
            for name in ["lumen-api", "lumen-worker", "lumen-redis", "lumen-web"]
        )
        if api_after and not api_after.issubset(api_before) and all_sidecars_alive and wait_until_ready(1):
            api_restarted = True
            break
        if not process_alive(app_pid):
            break
        time.sleep(0.25)

logs = {
    "supervisor.log": read_log("supervisor.log"),
    "redis.log": read_log("redis.log"),
    "redis.err.log": read_log("redis.err.log"),
    "api.log": read_log("api.log"),
    "api.err.log": read_log("api.err.log"),
    "worker.err.log": read_log("worker.err.log"),
    "web.log": read_log("web.log"),
    "web.err.log": read_log("web.err.log"),
}
combined = "\n".join(logs.values())
ps_out = subprocess.check_output(
    ["ps", "ax", "-o", "pid,ppid,command"], text=True, errors="replace"
)
processes = {
    name: bool(sidecar_pids(name))
    for name in ["lumen-api", "lumen-worker", "lumen-redis", "lumen-web"]
}

print(f"logs_root={logs_root}")
print(f"api_port={api_port} web_port={web_port}")
print(f"baseline_ready={str(baseline_ready).lower()}")
print(f"worker_restarted={str(worker_restarted).lower()}")
print(f"web_restarted={str(web_restarted).lower()}")
print(f"api_restarted={str(api_restarted).lower()}")
print(
    "processes "
    + " ".join(f"{name}={str(alive).lower()}" for name, alive in processes.items())
)
for name, text in logs.items():
    print(f"--- {name} tail ---")
    print(text[-1600:])

errors: list[str] = []
if "--logdir" in combined or "LogDir specified without enabling tiered storage" in combined:
    errors.append("old Garnet logdir failure is present")
if "api_key is required" in logs["worker.err.log"]:
    errors.append("worker rejects disabled desktop provider without api_key")
if '"event":"heartbeat"' not in logs["supervisor.log"]:
    errors.append("supervisor heartbeat event was not logged")
if '"event":"sidecar_restart"' not in logs["supervisor.log"]:
    errors.append("supervisor sidecar_restart event was not logged")
if '"event":"full_restart"' not in logs["supervisor.log"]:
    errors.append("supervisor full_restart event was not logged")
if not baseline_ready:
    errors.append("baseline desktop readiness was not reached")
if "Ready to accept connections" not in logs["redis.log"]:
    errors.append("redis readiness not proven")
if not worker_before:
    errors.append("worker process was not present before restart probe")
elif not worker_restarted:
    errors.append("worker process did not restart after termination")
if not web_before:
    errors.append("web process was not present before restart probe")
elif not web_restarted:
    errors.append("web process did not restart after termination")
if not api_before:
    errors.append("api process was not present before critical restart probe")
elif not api_restarted:
    errors.append("api critical restart did not recover the full stack")
if api_port is None or web_port is None:
    errors.append("api/web ports not discovered")
else:
    try:
        if get_http(api_port, "/system/desktop-ready") != 200:
            errors.append("api desktop-ready did not return 200")
    except Exception as exc:
        errors.append(f"api desktop-ready request failed: {exc}")
    try:
        if get_http(web_port, "/") != 200:
            errors.append("web root did not return 200")
    except Exception as exc:
        errors.append(f"web root request failed: {exc}")
    try:
        if get_http(web_port, "/api/auth/me") != 200:
            errors.append("web proxy auth/me did not return 200")
    except Exception as exc:
        errors.append(f"web proxy auth/me request failed: {exc}")
    try:
        if get_http(web_port, "/api/conversations?limit=1") != 200:
            errors.append("web proxy conversations did not return 200")
    except Exception as exc:
        errors.append(f"web proxy conversations request failed: {exc}")
if not all(processes.values()):
    errors.append("not all sidecar processes are alive")

if errors:
    print("--- process candidates ---")
    markers = ["Lumen.app", "server.js"]
    if web_port is not None:
        markers.append(str(web_port))
    for line in ps_out.splitlines():
        if any(marker in line for marker in markers):
            print(line)
    print("app_stdout_tail=")
    print((work / "app.stdout.log").read_text(errors="replace")[-2000:])
    print("app_stderr_tail=")
    print((work / "app.stderr.log").read_text(errors="replace")[-2000:])
    for error in errors:
        print(f"ERROR: {error}")
    sys.exit(1)

print("dmg_launch_smoke_ok")
PY
