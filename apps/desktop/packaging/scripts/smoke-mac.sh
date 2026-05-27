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
  pkill -f "$mount/Lumen.app/Contents/.*lumen-(api|worker|redis)" >/dev/null 2>&1 || true
  pkill -f "$mount/Lumen.app/Contents/.*server\\.js" >/dev/null 2>&1 || true
  hdiutil detach "$mount" -quiet >/dev/null 2>&1 \
    || hdiutil detach "$mount" -force -quiet >/dev/null 2>&1 \
    || true
  if ! diskutil info -plist "$mount" >/dev/null 2>&1; then
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
codesign --verify --deep --strict --verbose=2 "$app"
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
import base64
import json
import pathlib
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

home = pathlib.Path(os.environ["HOME_DIR"])
work = pathlib.Path(os.environ["WORK_DIR"])
mount = os.environ["MOUNT_DIR"]
app_pid = int(os.environ["APP_PID"])
mount_markers = {mount, os.path.realpath(mount)}
logs_root = home / "Library/Application Support/com.lumen.desktop/data/logs"
api_port = None
web_port = None
HTTP_TIMEOUT_SECONDS = 8


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


no_redirect_opener = urllib.request.build_opener(NoRedirect)


def read_log(name):
    path = logs_root / name
    return path.read_text(errors="replace") if path.exists() else ""


def process_alive(pid):
    return (
        subprocess.run(
            ["kill", "-0", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def http_request(
    port,
    path,
    method="GET",
    body=None,
    headers=None,
    follow_redirects=True,
    raw_body=None,
):
    request_headers = {"Connection": "close"}
    data = None
    if body is not None and raw_body is not None:
        raise ValueError("body and raw_body are mutually exclusive")
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
        request_headers["Accept"] = "application/json"
    elif raw_body is not None:
        data = raw_body
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    opener = urllib.request.urlopen if follow_redirects else no_redirect_opener.open
    try:
        with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(4096)
            return response.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096)
        return exc.code, raw.decode("utf-8", "replace")


def get_http(port, path, headers=None, follow_redirects=True):
    status, _ = http_request(
        port,
        path,
        headers=headers,
        follow_redirects=follow_redirects,
    )
    return status


def json_request(port, path, method="GET", body=None, headers=None):
    status, text = http_request(
        port,
        path,
        method=method,
        body=body,
        headers=headers,
    )
    try:
        payload = json.loads(text) if text else None
    except json.JSONDecodeError:
        payload = None
    return status, payload


def multipart_file_request(port, path, field_name, filename, content_type, data):
    boundary = f"----lumen-desktop-smoke-{int(time.time() * 1000)}"
    raw = b"".join(
        [
            f"--{boundary}\r\n".encode("ascii"),
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode("ascii"),
            f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
            data,
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ]
    )
    status, text = http_request(
        port,
        path,
        method="POST",
        raw_body=raw,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    try:
        payload = json.loads(text) if text else None
    except json.JSONDecodeError:
        payload = None
    return status, payload


def listening_pids(port):
    try:
        output = subprocess.check_output(
            ["lsof", "-nP", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            text=True,
            errors="replace",
        )
    except subprocess.CalledProcessError:
        return []
    pids = []
    for line in output.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            pass
    return pids


def sidecar_pids(name):
    if name == "lumen-web" and web_port is not None:
        pids = listening_pids(web_port)
        if pids:
            return pids
    ps_output = subprocess.check_output(
        ["ps", "ax", "-o", "pid,ppid,command"], text=True, errors="replace"
    )
    pids = []
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


def wait_until_ready(seconds):
    global api_port, web_port
    deadline = time.time() + seconds
    while time.time() < deadline:
        api_err = read_log("api.err.log")
        web_log = read_log("web.log")
        match = re.search(r"Uvicorn running on http://127\.0\.0\.1:(\d+)", api_err)
        if match:
            api_port = int(match.group(1))
        match = re.search(r"Local:\s+http://(?:localhost|127\.0\.0\.1):(\d+)", web_log)
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
operation_errors = []
if baseline_ready and web_port is not None:
    try:
        status, conversation = json_request(
            web_port,
            "/api/conversations",
            method="POST",
            body={"title": "desktop smoke"},
        )
        conv_id = conversation.get("id") if isinstance(conversation, dict) else None
        if status != 200 or not conv_id:
            operation_errors.append("desktop conversation create did not return an id")
        else:
            escaped_id = urllib.parse.quote(str(conv_id), safe="")
            status, patched = json_request(
                web_port,
                f"/api/conversations/{escaped_id}",
                method="PATCH",
                body={"title": "desktop smoke updated"},
            )
            if status != 200 or not isinstance(patched, dict) or patched.get("title") != "desktop smoke updated":
                operation_errors.append("desktop conversation patch did not persist title")
            status, _ = json_request(web_port, f"/api/conversations/{escaped_id}")
            if status != 200:
                operation_errors.append(f"desktop conversation get returned {status}")
            status, deleted = json_request(
                web_port,
                f"/api/conversations/{escaped_id}",
                method="DELETE",
            )
            if status != 200 or not isinstance(deleted, dict) or deleted.get("ok") is not True:
                operation_errors.append("desktop conversation delete did not return ok=true")
    except Exception as exc:
        operation_errors.append(f"desktop conversation CRUD request failed: {exc}")
    try:
        status, prompts = json_request(web_port, "/api/system-prompts")
        if status != 200 or not isinstance(prompts, dict):
            operation_errors.append("desktop system prompts list did not return 200")
        status, prompt = json_request(
            web_port,
            "/api/system-prompts",
            method="POST",
            body={
                "name": "Desktop Smoke Prompt",
                "content": "You are a desktop smoke test.",
                "make_default": True,
            },
        )
        prompt_id = prompt.get("id") if isinstance(prompt, dict) else None
        if status != 200 or not prompt_id or prompt.get("is_default") is not True:
            operation_errors.append("desktop system prompt create did not return a default prompt")
        else:
            escaped_prompt_id = urllib.parse.quote(str(prompt_id), safe="")
            status, patched = json_request(
                web_port,
                f"/api/system-prompts/{escaped_prompt_id}",
                method="PATCH",
                body={
                    "name": "Desktop Smoke Prompt Updated",
                    "content": "Updated desktop smoke prompt.",
                    "make_default": False,
                },
            )
            if (
                status != 200
                or not isinstance(patched, dict)
                or patched.get("name") != "Desktop Smoke Prompt Updated"
            ):
                operation_errors.append("desktop system prompt patch did not persist name")
            status, defaulted = json_request(
                web_port,
                f"/api/system-prompts/{escaped_prompt_id}/default",
                method="POST",
            )
            if status != 200 or not isinstance(defaulted, dict) or defaulted.get("is_default") is not True:
                operation_errors.append("desktop system prompt default did not persist")
            status, _ = json_request(
                web_port,
                f"/api/system-prompts/{escaped_prompt_id}",
                method="DELETE",
            )
            if status != 204:
                operation_errors.append(f"desktop system prompt delete returned {status}")
    except Exception as exc:
        operation_errors.append(f"desktop system prompt CRUD request failed: {exc}")
    try:
        provider_name = "desktop-smoke-provider"
        provider_payload = {
            "items": [
                {
                    "name": provider_name,
                    "base_url": "http://127.0.0.1:9",
                    "api_key": "sk-desktop-smoke-key",
                    "priority": 0,
                    "weight": 1,
                    "enabled": True,
                    "purposes": ["chat", "image"],
                    "image_jobs_enabled": True,
                    "image_jobs_endpoint": "generations",
                    "image_jobs_endpoint_lock": True,
                    "image_jobs_base_url": "",
                    "image_edit_input_transport": "url",
                    "image_concurrency": 1,
                }
            ],
            "proxies": [],
        }
        status, providers = json_request(
            web_port,
            "/api/settings/providers",
            method="PUT",
            body=provider_payload,
        )
        items = providers.get("items") if isinstance(providers, dict) else None
        first_provider = items[0] if isinstance(items, list) and items else None
        if (
            status != 200
            or not isinstance(first_provider, dict)
            or first_provider.get("name") != provider_name
            or first_provider.get("enabled") is not True
            or first_provider.get("api_key_hint") == "sk-desktop-smoke-key"
        ):
            operation_errors.append("desktop providers PUT did not persist masked provider")
        status, probe = json_request(
            web_port,
            "/api/settings/providers/probe",
            method="POST",
            body={"names": [provider_name]},
        )
        probe_items = probe.get("items") if isinstance(probe, dict) else None
        first_probe = probe_items[0] if isinstance(probe_items, list) and probe_items else None
        if (
            status != 200
            or not isinstance(first_probe, dict)
            or first_probe.get("name") != provider_name
            or first_probe.get("status") != "skipped"
            or first_probe.get("error") != "endpoint_locked_to_generations"
        ):
            operation_errors.append("desktop providers probe did not skip generation-locked provider")
        escaped_provider_name = urllib.parse.quote(provider_name, safe="")
        status, disabled_provider = json_request(
            web_port,
            f"/api/settings/providers/{escaped_provider_name}/enabled",
            method="PATCH",
            body={"enabled": False},
        )
        if (
            status != 200
            or not isinstance(disabled_provider, dict)
            or disabled_provider.get("enabled") is not False
        ):
            operation_errors.append("desktop provider enabled PATCH did not persist false")
        status, stats = json_request(web_port, "/api/settings/providers/stats")
        stat_items = stats.get("items") if isinstance(stats, dict) else None
        if (
            status != 200
            or not isinstance(stat_items, list)
            or not any(isinstance(item, dict) and item.get("name") == provider_name for item in stat_items)
        ):
            operation_errors.append("desktop provider stats did not include saved provider")
        status, cleared = json_request(
            web_port,
            "/api/settings/providers",
            method="PUT",
            body={"items": [], "proxies": []},
        )
        cleared_items = cleared.get("items") if isinstance(cleared, dict) else None
        if status != 200 or cleared_items != []:
            operation_errors.append("desktop providers clear did not return empty items")
    except Exception as exc:
        operation_errors.append(f"desktop providers save/probe/clear request failed: {exc}")
    try:
        status, settings = json_request(web_port, "/api/me/memory-settings")
        if status != 200 or not isinstance(settings, dict):
            operation_errors.append("desktop memory settings did not return 200")
        status, settings = json_request(
            web_port,
            "/api/me/memory-settings",
            method="PATCH",
            body={"paused": True, "confirmation_enabled": True},
        )
        if (
            status != 200
            or not isinstance(settings, dict)
            or settings.get("paused") is not True
            or settings.get("confirmation_enabled") is not True
        ):
            operation_errors.append("desktop memory settings patch did not persist")
        status, onboarding = json_request(
            web_port,
            "/api/me/onboarding-seen",
            method="PATCH",
            body={"flag": 2},
        )
        if (
            status != 200
            or not isinstance(onboarding, dict)
            or (int(onboarding.get("onboarding_seen") or 0) & (1 << 2)) == 0
        ):
            operation_errors.append("desktop memory onboarding flag did not persist")
        status, scopes = json_request(web_port, "/api/me/memory-scopes")
        if status != 200 or not isinstance(scopes, list):
            operation_errors.append("desktop memory scopes list did not return 200")
        status, scope = json_request(
            web_port,
            "/api/me/memory-scopes",
            method="POST",
            body={"name": "Desktop Smoke Scope", "emoji": "DS"},
        )
        scope_id = scope.get("id") if isinstance(scope, dict) else None
        if status != 200 or not scope_id:
            operation_errors.append("desktop memory scope create did not return an id")
        else:
            escaped_scope_id = urllib.parse.quote(str(scope_id), safe="")
            status, patched_scope = json_request(
                web_port,
                f"/api/me/memory-scopes/{escaped_scope_id}",
                method="PATCH",
                body={"name": "Desktop Smoke Scope Renamed", "emoji": "DR"},
            )
            if (
                status != 200
                or not isinstance(patched_scope, dict)
                or patched_scope.get("name") != "Desktop Smoke Scope Renamed"
                or patched_scope.get("emoji") != "DR"
            ):
                operation_errors.append("desktop memory scope patch did not persist")
            status, memory_conv = json_request(
                web_port,
                "/api/conversations",
                method="POST",
                body={"title": "desktop memory smoke"},
            )
            memory_conv_id = (
                memory_conv.get("id") if isinstance(memory_conv, dict) else None
            )
            if status != 200 or not memory_conv_id:
                operation_errors.append("desktop memory conversation create failed")
            else:
                escaped_memory_conv_id = urllib.parse.quote(
                    str(memory_conv_id), safe=""
                )
                status, active_scope = json_request(
                    web_port,
                    f"/api/conversations/{escaped_memory_conv_id}/active-scope",
                    method="PATCH",
                    body={"scope_id": str(scope_id)},
                )
                if (
                    status != 200
                    or not isinstance(active_scope, dict)
                    or active_scope.get("scope_id") != scope_id
                ):
                    operation_errors.append(
                        "desktop conversation active memory scope did not persist"
                    )
                status, memory_disabled = json_request(
                    web_port,
                    f"/api/conversations/{escaped_memory_conv_id}/memory-disabled",
                    method="PATCH",
                    body={"disabled": True},
                )
                if (
                    status != 200
                    or not isinstance(memory_disabled, dict)
                    or memory_disabled.get("disabled") is not True
                ):
                    operation_errors.append(
                        "desktop conversation memory disable did not persist"
                    )
                status, used_memories = json_request(
                    web_port,
                    f"/api/conversations/{escaped_memory_conv_id}/used-memories",
                )
                if status != 200 or not isinstance(used_memories, dict):
                    operation_errors.append(
                        "desktop conversation used memories did not return 200"
                    )
            status, memory = json_request(
                web_port,
                "/api/me/memories",
                method="POST",
                body={
                    "type": "preference",
                    "content": "Desktop smoke memory preference",
                    "pinned": True,
                    "scope_id": str(scope_id),
                },
            )
            memory_id = memory.get("id") if isinstance(memory, dict) else None
            if status != 200 or not memory_id or memory.get("pinned") is not True:
                operation_errors.append("desktop memory create did not return a pinned memory")
            else:
                escaped_memory_id = urllib.parse.quote(str(memory_id), safe="")
                status, patched_memory = json_request(
                    web_port,
                    f"/api/me/memories/{escaped_memory_id}",
                    method="PATCH",
                    body={
                        "content": "Desktop smoke memory updated",
                        "pinned": False,
                    },
                )
                if (
                    status != 200
                    or not isinstance(patched_memory, dict)
                    or patched_memory.get("content") != "Desktop smoke memory updated"
                    or patched_memory.get("pinned") is not False
                ):
                    operation_errors.append("desktop memory patch did not persist")
                status, scoped_memory = json_request(
                    web_port,
                    f"/api/me/memories/{escaped_memory_id}/scope",
                    method="PATCH",
                    body={"scope_id": str(scope_id)},
                )
                if (
                    status != 200
                    or not isinstance(scoped_memory, dict)
                    or scoped_memory.get("scope_id") != scope_id
                ):
                    operation_errors.append(
                        "desktop memory scope assignment did not persist"
                    )
                status, confirmed_memory = json_request(
                    web_port,
                    f"/api/me/memories/{escaped_memory_id}/confirm",
                    method="POST",
                    body={"decision": "yes"},
                )
                if (
                    status != 200
                    or not isinstance(confirmed_memory, dict)
                    or confirmed_memory.get("last_confirmed_at") is None
                ):
                    operation_errors.append("desktop memory confirm did not persist")
                status, memories = json_request(
                    web_port,
                    f"/api/me/memories?type=preference&pinned=false&disabled=false&scope_id={escaped_scope_id}",
                )
                memory_items = memories.get("items") if isinstance(memories, dict) else None
                if (
                    status != 200
                    or not isinstance(memory_items, list)
                    or not any(
                        isinstance(item, dict) and item.get("id") == memory_id
                        for item in memory_items
                    )
                ):
                    operation_errors.append(
                        "desktop memories filtered list did not include saved memory"
                    )
                status, staging = json_request(web_port, "/api/me/memories/staging")
                if (
                    status != 200
                    or not isinstance(staging, dict)
                    or not isinstance(staging.get("items"), list)
                ):
                    operation_errors.append("desktop memory staging list did not return 200")
                status, timeline = json_request(
                    web_port,
                    "/api/me/memories/timeline?limit=5",
                )
                if (
                    status != 200
                    or not isinstance(timeline, dict)
                    or not isinstance(timeline.get("items"), list)
                    or len(timeline.get("items")) < 1
                ):
                    operation_errors.append("desktop memory timeline did not include audit rows")
                status, exported = json_request(web_port, "/api/me/memories/export")
                if status != 200 or not isinstance(exported, dict):
                    operation_errors.append("desktop memories export did not return 200")
                status, clear_memory = json_request(
                    web_port,
                    "/api/me/memories",
                    method="POST",
                    body={
                        "type": "avoid",
                        "content": "Desktop smoke memory to clear",
                        "scope_id": str(scope_id),
                    },
                )
                clear_memory_id = (
                    clear_memory.get("id") if isinstance(clear_memory, dict) else None
                )
                if status != 200 or not clear_memory_id:
                    operation_errors.append(
                        "desktop memory clear fixture create did not return an id"
                    )
                status, deleted_memory = json_request(
                    web_port,
                    f"/api/me/memories/{escaped_memory_id}",
                    method="DELETE",
                )
                if status != 200 or not isinstance(deleted_memory, dict) or deleted_memory.get("ok") is not True:
                    operation_errors.append("desktop memory delete did not return ok=true")
                status, cleared_memories = json_request(
                    web_port,
                    "/api/me/memories",
                    method="DELETE",
                    headers={"X-Confirm-Clear-Memory": "yes"},
                )
                if (
                    status != 200
                    or not isinstance(cleared_memories, dict)
                    or int(cleared_memories.get("deleted") or 0) < 1
                ):
                    operation_errors.append("desktop memory clear did not delete rows")
            if memory_conv_id:
                json_request(
                    web_port,
                    f"/api/conversations/{urllib.parse.quote(str(memory_conv_id), safe='')}",
                    method="DELETE",
                )
            status, deleted_scope = json_request(
                web_port,
                f"/api/me/memory-scopes/{escaped_scope_id}",
                method="DELETE",
            )
            if status != 200 or not isinstance(deleted_scope, dict) or "moved" not in deleted_scope:
                operation_errors.append("desktop memory scope delete did not return moved count")
    except Exception as exc:
        operation_errors.append(f"desktop memory CRUD request failed: {exc}")
    try:
        status, feed = json_request(web_port, "/api/generations/feed?limit=1")
        if (
            status != 200
            or not isinstance(feed, dict)
            or not isinstance(feed.get("items"), list)
            or not isinstance(feed.get("total"), int)
        ):
            operation_errors.append("desktop generations feed did not return an item list")
        status, _ = json_request(web_port, "/api/generations/feed?ratio=bad-ratio")
        if status != 400:
            operation_errors.append(f"desktop generations feed invalid ratio returned {status}")
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg=="
        )
        status, uploaded = multipart_file_request(
            web_port,
            "/api/images/upload",
            "file",
            "desktop-smoke.png",
            "image/png",
            png_bytes,
        )
        image_id = uploaded.get("id") if isinstance(uploaded, dict) else None
        if (
            status != 200
            or not image_id
            or uploaded.get("width") != 2
            or uploaded.get("height") != 2
            or uploaded.get("mime") != "image/png"
            or uploaded.get("url") != f"/api/images/{image_id}/binary"
        ):
            operation_errors.append("desktop image upload did not return expected metadata")
        else:
            escaped_image_id = urllib.parse.quote(str(image_id), safe="")
            status, meta = json_request(web_port, f"/api/images/{escaped_image_id}")
            metadata_jsonb = meta.get("metadata_jsonb") if isinstance(meta, dict) else None
            if (
                status != 200
                or not isinstance(meta, dict)
                or meta.get("id") != image_id
                or not isinstance(metadata_jsonb, dict)
                or "normalized_ref" not in metadata_jsonb
            ):
                operation_errors.append("desktop image metadata did not include normalized_ref")
            if get_http(web_port, f"/api/images/{escaped_image_id}/binary") != 200:
                operation_errors.append("desktop image binary did not return 200")
            if get_http(web_port, f"/api/images/{escaped_image_id}/variants/display2048") != 200:
                operation_errors.append("desktop image display variant did not return 200")
            status, share = json_request(
                web_port,
                f"/api/images/{escaped_image_id}/share",
                method="POST",
                body={"show_prompt": False},
            )
            share_id = share.get("id") if isinstance(share, dict) else None
            share_token = share.get("token") if isinstance(share, dict) else None
            if status != 201 or not share_id or not share_token or share.get("image_id") != image_id:
                operation_errors.append("desktop image share create did not return a token")
            else:
                escaped_share_id = urllib.parse.quote(str(share_id), safe="")
                escaped_share_token = urllib.parse.quote(str(share_token), safe="")
                status, share_list = json_request(web_port, "/api/me/shares")
                share_items = share_list.get("items") if isinstance(share_list, dict) else None
                if (
                    status != 200
                    or not isinstance(share_items, list)
                    or not any(
                        isinstance(item, dict) and item.get("id") == share_id
                        for item in share_items
                    )
                ):
                    operation_errors.append("desktop share list did not include created share")
                status, public_share = json_request(web_port, f"/api/share/{escaped_share_token}")
                public_images = (
                    public_share.get("images") if isinstance(public_share, dict) else None
                )
                first_public_image = (
                    public_images[0] if isinstance(public_images, list) and public_images else None
                )
                if (
                    status != 200
                    or not isinstance(public_share, dict)
                    or public_share.get("token") != share_token
                    or not isinstance(first_public_image, dict)
                    or first_public_image.get("id") != image_id
                ):
                    operation_errors.append("desktop public share metadata did not include uploaded image")
                else:
                    display_url = first_public_image.get("display_url")
                    if not isinstance(display_url, str) or not display_url.startswith("/api/share/"):
                        operation_errors.append("desktop public share metadata did not include display variant")
                    elif get_http(web_port, display_url) != 200:
                        operation_errors.append("desktop public share display variant did not return 200")
                if get_http(web_port, f"/api/share/{escaped_share_token}/image") != 200:
                    operation_errors.append("desktop public share image did not return 200")
                if get_http(web_port, f"/api/share/{escaped_share_token}/images/{escaped_image_id}") != 200:
                    operation_errors.append("desktop public share image-by-id did not return 200")
                if (
                    get_http(
                        web_port,
                        f"/api/share/{escaped_share_token}/images/{escaped_image_id}/variants/bad-kind",
                    )
                    != 400
                ):
                    operation_errors.append("desktop public share invalid variant did not return 400")
                if get_http(web_port, f"/share/{escaped_share_token}") != 200:
                    operation_errors.append("desktop share page did not return 200")
                status, _ = json_request(
                    web_port,
                    f"/api/shares/{escaped_share_id}",
                    method="DELETE",
                )
                if status != 204:
                    operation_errors.append(f"desktop share revoke returned {status}")
                status, _ = json_request(web_port, f"/api/share/{escaped_share_token}")
                if status != 404:
                    operation_errors.append("desktop revoked share did not return 404")
            status, multi_share = json_request(
                web_port,
                "/api/images/share",
                method="POST",
                body={"image_ids": [image_id], "show_prompt": False},
            )
            multi_share_id = multi_share.get("id") if isinstance(multi_share, dict) else None
            multi_share_token = multi_share.get("token") if isinstance(multi_share, dict) else None
            multi_image_ids = multi_share.get("image_ids") if isinstance(multi_share, dict) else None
            if (
                status != 201
                or not multi_share_id
                or not multi_share_token
                or multi_image_ids != [image_id]
            ):
                operation_errors.append("desktop multi-image share create did not return image_ids")
            else:
                escaped_multi_share_id = urllib.parse.quote(str(multi_share_id), safe="")
                escaped_multi_share_token = urllib.parse.quote(str(multi_share_token), safe="")
                if (
                    get_http(
                        web_port,
                        f"/api/share/{escaped_multi_share_token}/images/{escaped_image_id}",
                    )
                    != 200
                ):
                    operation_errors.append("desktop multi-image public image-by-id did not return 200")
                status, _ = json_request(
                    web_port,
                    f"/api/shares/{escaped_multi_share_id}",
                    method="DELETE",
                )
                if status != 204:
                    operation_errors.append(f"desktop multi-image share revoke returned {status}")
            status, deleted_image = json_request(
                web_port,
                f"/api/images/{escaped_image_id}",
                method="DELETE",
            )
            if status != 200 or not isinstance(deleted_image, dict) or deleted_image.get("ok") is not True:
                operation_errors.append("desktop image delete did not return ok=true")
            if get_http(web_port, f"/api/images/{escaped_image_id}/binary") != 404:
                operation_errors.append("desktop image binary after delete did not return 404")
    except Exception as exc:
        operation_errors.append(f"desktop image and feed requests failed: {exc}")
else:
    operation_errors.append("desktop conversation CRUD skipped before baseline readiness")
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
    os.kill(next(iter(web_before)), signal.SIGKILL)
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

if api_restarted:
    wait_until_ready(10)
    time.sleep(2.0)

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

errors = list(operation_errors)
if "--logdir" in combined or "LogDir specified without enabling tiered storage" in combined:
    errors.append("old Garnet logdir failure is present")
if "api_key is required" in logs["worker.err.log"]:
    errors.append("worker rejects disabled desktop provider without api_key")
if "context_window.tiktoken_unavailable" in combined:
    errors.append("packaged Python runtime could not load tiktoken")
if "context_window.tiktoken_loading_slow" in combined:
    errors.append("packaged Python runtime fell back before tiktoken warmed")
if "Lua scripting support disabled" in combined:
    errors.append("redis lua scripting is disabled")
if "Unknown Redis command called from script" in combined or "sse dedupe reservation has no stream id" in combined:
    errors.append("redis lua xadd fallback did not handle Garnet")
if (
    "api publish_sse_event xadd failed" in combined
    or "api publish_sse_events xadd batch failed" in combined
    or "publish_event: XADD failed" in combined
):
    errors.append("redis stream xadd fallback did not handle Garnet")
if re.search(r"Network:\s+http://(?!localhost(?::|/)|127\.0\.0\.1(?::|/))", logs["web.log"]) or "0.0.0.0" in logs["web.log"]:
    errors.append("web runtime is listening on a non-loopback interface")
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
        if get_http(api_port, "/auth/me") != 401:
            errors.append("direct api auth/me without desktop token did not return 401")
    except Exception as exc:
        errors.append(f"direct api auth/me request failed: {exc}")
    try:
        if get_http(api_port, "/system/desktop-activity") != 401:
            errors.append("direct api desktop-activity without desktop token did not return 401")
    except Exception as exc:
        errors.append(f"direct api desktop-activity request failed: {exc}")
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
    try:
        if get_http(web_port, "/api/system/desktop-activity") != 200:
            errors.append("web proxy desktop-activity did not return 200")
    except Exception as exc:
        errors.append(f"web proxy desktop-activity request failed: {exc}")
    desktop_routes = [
        "/",
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
    for route in desktop_routes:
        try:
            if get_http(web_port, route) != 200:
                errors.append(f"desktop web route {route} did not return 200")
        except Exception as exc:
            errors.append(f"desktop web route {route} request failed: {exc}")
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
    for route in docker_only_routes:
        try:
            status = get_http(web_port, route, follow_redirects=False)
            if status not in (301, 302, 303, 307, 308):
                errors.append(f"desktop unsupported route {route} did not redirect")
        except Exception as exc:
            errors.append(f"desktop unsupported route {route} request failed: {exc}")
    api_gets = {
        "/api/auth/me": 200,
        "/api/auth/csrf": 200,
        "/api/settings/bootstrap-status": 200,
        "/api/settings/diagnostics": 200,
        "/api/settings/system": 200,
        "/api/settings/providers": 200,
        "/api/settings/providers/stats": 200,
        "/api/conversations?limit=1": 200,
        "/api/generations/feed?limit=1": 200,
        "/api/system/desktop-activity": 200,
    }
    for path, expected in api_gets.items():
        try:
            status = get_http(web_port, path)
            if status != expected:
                errors.append(f"desktop web proxy {path} returned {status}, expected {expected}")
        except Exception as exc:
            errors.append(f"desktop web proxy {path} request failed: {exc}")
    try:
        status, payload = json_request(web_port, "/api/auth/csrf")
        if (
            status != 200
            or not isinstance(payload, dict)
            or payload.get("csrf_token") != "desktop-local-token"
        ):
            errors.append("desktop csrf did not return desktop-local-token")
    except Exception as exc:
        errors.append(f"desktop csrf request failed: {exc}")
    try:
        status, _ = json_request(web_port, "/api/auth/logout", method="POST")
        if status != 204:
            errors.append(f"desktop logout returned {status}")
        if get_http(web_port, "/api/auth/me") != 200:
            errors.append("desktop auth/me failed after logout no-op")
    except Exception as exc:
        errors.append(f"desktop logout request failed: {exc}")
    try:
        status, payload = json_request(
            web_port,
            "/api/settings/bootstrap-complete",
            method="POST",
            body={
                "settings": {
                    "theme": "system",
                    "language": "zh-CN",
                    "auto_check_updates": True,
                    "crash_reports_enabled": False,
                }
            },
        )
        if status != 200 or not isinstance(payload, dict) or payload.get("complete") is not True:
            errors.append("desktop bootstrap-complete did not return complete=true")
    except Exception as exc:
        errors.append(f"desktop bootstrap-complete request failed: {exc}")
    try:
        status, payload = json_request(web_port, "/api/settings/bootstrap-status")
        if status != 200 or not isinstance(payload, dict) or payload.get("complete") is not True:
            errors.append("desktop bootstrap status did not persist complete=true")
    except Exception as exc:
        errors.append(f"desktop bootstrap-status request failed: {exc}")
    try:
        status, diagnostics = json_request(web_port, "/api/settings/diagnostics")
        data_root = diagnostics.get("data_root") if isinstance(diagnostics, dict) else None
        logs_root_value = diagnostics.get("logs_root") if isinstance(diagnostics, dict) else None
        settings_path = diagnostics.get("settings_path") if isinstance(diagnostics, dict) else None
        provider_metadata_path = (
            diagnostics.get("provider_metadata_path") if isinstance(diagnostics, dict) else None
        )
        disk_free_bytes = diagnostics.get("disk_free_bytes") if isinstance(diagnostics, dict) else None
        if (
            status != 200
            or data_root != str(home / "Library/Application Support/com.lumen.desktop")
            or not isinstance(logs_root_value, str)
            or not logs_root_value.endswith("/data/logs")
            or not isinstance(settings_path, str)
            or not settings_path.endswith("/data/settings.json")
            or not isinstance(provider_metadata_path, str)
            or not provider_metadata_path.endswith("/data/providers.json")
            or diagnostics.get("bootstrap_complete") is not True
            or not isinstance(disk_free_bytes, int)
            or disk_free_bytes <= 0
        ):
            errors.append("desktop diagnostics payload did not match runtime state")
    except Exception as exc:
        errors.append(f"desktop diagnostics request failed: {exc}")
    try:
        status, _ = json_request(
            web_port,
            "/api/settings/system",
            method="PUT",
            body={
                "items": [
                    {"key": "providers.auto_probe_interval", "value": "0"},
                    {"key": "providers.auto_image_probe_interval", "value": "0"},
                ]
            },
        )
        if status != 200:
            errors.append(f"desktop settings/system PUT returned {status}")
    except Exception as exc:
        errors.append(f"desktop settings/system PUT request failed: {exc}")
    try:
        status, _ = json_request(
            web_port,
            "/api/settings/system",
            method="PUT",
            body={"items": [{"key": "billing.enabled", "value": "true"}]},
        )
        if status != 422:
            errors.append(f"desktop settings/system unsupported key returned {status}")
    except Exception as exc:
        errors.append(f"desktop settings/system unsupported key request failed: {exc}")
    try:
        status, _ = json_request(
            web_port,
            "/api/settings/system",
            method="PUT",
            body={"items": [{"key": "providers.auto_probe_interval", "value": "not-an-int"}]},
        )
        if status != 422:
            errors.append(f"desktop settings/system invalid value returned {status}")
    except Exception as exc:
        errors.append(f"desktop settings/system invalid value request failed: {exc}")
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
