"""Render the host-side release rollback script."""

from __future__ import annotations

from pathlib import Path
import re
import shlex


_ROLLBACK_SCRIPT_BODY = r"""
ts() { date -u +%FT%TZ; }
elapsed_ms() {
  local started="$1"
  local ended
  ended="$(date +%s)"
  printf '%s' "$(((ended - started) * 1000))"
}
release_dir() { printf '%s/releases/%s' "$ROOT" "$1"; }
release_line() {
  local path="$1"
  [ -f "$path" ] || return 1
  head -n1 "$path" | tr -d '[:space:]'
}
env_value() {
  local key="$1"
  local file="$2"
  local raw
  raw="$(sed -n "s/^${key}=//p" "$file" 2>/dev/null | head -n1 || true)"
  raw="${raw%$'\r'}"
  if [[ "$raw" == \'*\' && "$raw" == *\' ]]; then
    raw="${raw:1:${#raw}-2}"
  elif [[ "$raw" == \"*\" && "$raw" == *\" ]]; then
    raw="${raw:1:${#raw}-2}"
  fi
  printf '%s' "$raw"
}
canonical_path() {
  python3 - "$1" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).resolve(strict=True))
PY
}
same_path() {
  local left right
  left="$(canonical_path "$1" 2>/dev/null)" || return 1
  right="$(canonical_path "$2" 2>/dev/null)" || return 1
  [ "$left" = "$right" ]
}
link_release_id() {
  local link="$1"
  python3 - "$ROOT" "$link" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1]).resolve(strict=True)
link = Path(sys.argv[2])
release = link.resolve(strict=True)
releases = (root / "releases").resolve(strict=True)
if release.parent != releases or re.fullmatch(r"[0-9]{8}-[0-9]{6}", release.name) is None:
    raise SystemExit(1)
print(release.name)
PY
}
atomic_symlink() {
  local link="$1"
  local target="$2"
  python3 - "$link" "$target" <<'PY'
import os
from pathlib import Path
import sys

link = Path(sys.argv[1])
target = sys.argv[2]
temporary = link.parent / f".{link.name}.rollback.{os.getpid()}"
try:
    temporary.unlink(missing_ok=True)
    os.symlink(target, temporary)
    os.replace(temporary, link)
finally:
    temporary.unlink(missing_ok=True)
PY
}
set_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  python3 - "$file" "$key" "$value" <<'PY'
import os
from pathlib import Path
import re
import sys
import tempfile

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None or "\n" in value or "\r" in value:
    raise SystemExit(64)
stat = path.stat()
lines = path.read_text(encoding="utf-8").splitlines()
rendered = []
written = False
for line in lines:
    if line.startswith(key + "="):
        if not written:
            rendered.append(f"{key}={value}")
            written = True
        continue
    rendered.append(line)
if not written:
    rendered.append(f"{key}={value}")
fd, temporary_raw = tempfile.mkstemp(
    prefix=f".{path.name}.rollback.",
    suffix=".tmp",
    dir=path.parent,
)
temporary = Path(temporary_raw)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(rendered) + "\n")
        handle.flush()
        os.fchmod(handle.fileno(), stat.st_mode & 0o7777)
        try:
            os.fchown(handle.fileno(), stat.st_uid, stat.st_gid)
        except PermissionError:
            pass
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    temporary.unlink(missing_ok=True)
PY
}
persist_manual_marker() {
  local state="$1"
  python3 - "$MARKER_PATH" "$state" "$TARGET" "$ROLLBACK_START" "$ENV_BACKUP" <<'PY'
import os
from pathlib import Path
import sys
import tempfile

path = Path(sys.argv[1])
state = sys.argv[2]
target = sys.argv[3]
started_at = sys.argv[4]
env_backup = sys.argv[5]
if state not in {"failed_original_unhealthy", "manual_required"}:
    raise SystemExit(64)

values = {}
try:
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip()
except FileNotFoundError:
    pass

operation_id = values.get("operation_id") or f"rollback-{target}-{started_at}"
generation = values.get("generation", "0")
if not generation.isdigit():
    generation = "0"
lines = [
    "pid=0",
    f"started_at={values.get('started_at') or started_at}",
    f"operation_id={operation_id}",
    "owner=manual",
    f"state={state}",
    f"generation={generation}",
]
if Path(env_backup).is_file():
    lines.append(f"evidence_path={env_backup}")
payload = ("\n".join(lines) + "\n").encode()

path.parent.mkdir(parents=True, exist_ok=True)
fd, temporary_raw = tempfile.mkstemp(
    prefix=f".{path.name}.manual.",
    suffix=".tmp",
    dir=path.parent,
)
temporary = Path(temporary_raw)
try:
    os.fchmod(fd, 0o660)
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting manual rollback marker")
        view = view[written:]
    os.fsync(fd)
    os.close(fd)
    fd = -1
    os.replace(temporary, path)
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    if fd >= 0:
        os.close(fd)
    temporary.unlink(missing_ok=True)
PY
}
release_compose_path() {
  local dir
  dir="$(release_dir "$1")"
  local name
  for name in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
    if [ -f "$dir/$name" ]; then
      printf '%s' "$dir/$name"
      return 0
    fi
  done
  return 1
}
release_has_compose() { release_compose_path "$1" >/dev/null 2>&1; }
release_tgbot_expected() {
  [ -f "$SHARED_ENV" ] || return 1
  [ -n "$(env_value TELEGRAM_BOT_TOKEN "$SHARED_ENV")" ]
}
release_agent_runtime_expected() {
  grep -Eq '^[[:space:]]{2}agent-runtime:[[:space:]]*$' \
    "$(release_dir "$1")/docker-compose.yml" 2>/dev/null
}
validate_release_metadata() {
  python3 - "$(release_dir "$1")" "$1" <<'PY'
import json
from pathlib import Path
import re
import sys

release = Path(sys.argv[1])
expected_id = sys.argv[2]
metadata = json.loads((release / ".lumen_release.json").read_text(encoding="utf-8"))
if not isinstance(metadata, dict) or metadata.get("id") != expected_id:
    raise SystemExit(1)
sha = metadata.get("sha")
if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
    raise SystemExit(1)
tag = (release / ".image-tag").read_text(encoding="utf-8").strip()
version = (release / "VERSION").read_text(encoding="utf-8").strip()
if not tag or not version:
    raise SystemExit(1)
PY
}
release_identity_rows() {
  local id="$1"
  local expected_tgbot=0
  if release_tgbot_expected; then
    expected_tgbot=1
  fi
  python3 - "$(release_dir "$id")" "$id" "$expected_tgbot" <<'PY'
import json
from pathlib import Path
import re
import sys

release = Path(sys.argv[1])
expected_id = sys.argv[2]
expected_tgbot = sys.argv[3] == "1"
image_id_re = re.compile(r"sha256:[0-9a-f]{64}")
digest_ref_re = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
commit_re = re.compile(r"[0-9a-f]{40}")

metadata = json.loads((release / ".lumen_release.json").read_text(encoding="utf-8"))
proof = json.loads((release / ".update-image-proof.json").read_text(encoding="utf-8"))
tag = (release / ".image-tag").read_text(encoding="utf-8").strip()
if not isinstance(metadata, dict) or metadata.get("id") != expected_id:
    raise SystemExit("release metadata id mismatch")
source_commit = metadata.get("sha")
if not isinstance(source_commit, str) or commit_re.fullmatch(source_commit) is None:
    raise SystemExit("release metadata commit is invalid")
if not isinstance(proof, dict) or proof.get("schema") != 1:
    raise SystemExit("image proof schema is invalid")
if proof.get("source_commit") != source_commit or proof.get("target_tag") != tag:
    raise SystemExit("image proof release identity mismatch")
build = proof.get("build")
if not isinstance(build, bool):
    raise SystemExit("image proof build flag is invalid")
manifest_path = release / "release-manifest.json"
manifest_images = None
if manifest_path.exists():
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("commit_sha") != source_commit:
        raise SystemExit("release manifest commit mismatch")
    legacy_images = manifest.get("images")
    component_images = manifest.get("components", {})
    if not isinstance(legacy_images, dict) or not isinstance(component_images, dict):
        raise SystemExit("release manifest images are invalid")
    manifest_images = {**legacy_images, **component_images}

compose_services = proof.get("compose_services")
services = proof.get("services")
if not isinstance(compose_services, dict) or not isinstance(services, dict):
    raise SystemExit("image proof service maps are invalid")
required = ["api", "worker", "web"]
if re.search(r"(?m)^  agent-runtime:\s*$", (release / "docker-compose.yml").read_text(encoding="utf-8")):
    required.append("agent-runtime")
if expected_tgbot:
    required.append("tgbot")
for alias in ("api-green", "bootstrap", "migrate"):
    if alias in compose_services and compose_services[alias] != compose_services.get("api"):
        raise SystemExit(f"{alias} image proof does not match api")
for service in ("api", "worker", "agent-runtime", "web", "tgbot"):
    if service not in services:
        if service in required:
            raise SystemExit(f"missing image proof for {service}")
        continue
    record = services[service]
    image_id = compose_services.get(service)
    if (
        not isinstance(record, dict)
        or not isinstance(image_id, str)
        or image_id_re.fullmatch(image_id) is None
        or record.get("image_id") != image_id
        or record.get("service") != service
    ):
        raise SystemExit(f"invalid image proof for {service}")
    revision = record.get("revision")
    if not build and revision != source_commit:
        raise SystemExit(f"image/source release mismatch for {service}")
    repo_digests = record.get("repo_digests")
    immutable = sorted(
        value
        for value in repo_digests or []
        if isinstance(value, str) and digest_ref_re.fullmatch(value)
    )
    if not build and not immutable:
        raise SystemExit(f"missing immutable digest for {service}")
    source_ref = record.get("source_ref")
    if immutable:
        if not isinstance(source_ref, str) or not source_ref:
            raise SystemExit(f"missing source image reference for {service}")
        repository = source_ref.split("@", 1)[0]
        last_slash = repository.rfind("/")
        last_colon = repository.rfind(":")
        if last_colon > last_slash:
            repository = repository[:last_colon]
        if not all(value.startswith(repository + "@") for value in immutable):
            raise SystemExit(f"image digest repository mismatch for {service}")
    digest = immutable[0] if immutable else ""
    if manifest_images is not None:
        manifest_service = manifest_images.get(service)
        manifest_ref = (
            manifest_service.get("immutable_ref")
            if isinstance(manifest_service, dict)
            else None
        )
        if (
            not isinstance(manifest_ref, str)
            or digest_ref_re.fullmatch(manifest_ref) is None
            or manifest_ref not in immutable
        ):
            raise SystemExit(f"release manifest digest mismatch for {service}")
        digest = manifest_ref
    env_ref = digest or image_id
    print(f"{service}\t{image_id}\t{env_ref}\t{digest}")
PY
}
preflight_release() {
  local id="$1"
  local dir
  dir="$(release_dir "$id")"
  [ -d "$dir" ] || return 1
  validate_release_metadata "$id" || return 1
  if release_has_compose "$id"; then
    [ -f "$dir/.update-image-proof.json" ] || return 1
    [ -f "$dir/.update-images.override.yml" ] || return 1
    [ -f "$SHARED_ENV" ] || return 1
    same_path "$dir/.env" "$SHARED_ENV" || return 1
    release_identity_rows "$id" >/dev/null || return 1
  fi
}
apply_release_env() {
  local id="$1"
  local dir tag version rows service image_id env_ref digest key
  dir="$(release_dir "$id")"
  [ -f "$SHARED_ENV" ] || return 1
  tag="$(release_line "$dir/.image-tag")" || return 1
  version="$(release_line "$dir/VERSION")" || return 1
  set_env_value "$SHARED_ENV" LUMEN_IMAGE_TAG "$tag" || return 1
  set_env_value "$SHARED_ENV" LUMEN_VERSION "$version" || return 1
  if release_has_compose "$id"; then
    rows="$(release_identity_rows "$id")" || return 1
    while IFS=$'\t' read -r service image_id env_ref digest; do
      [ -n "$service" ] || continue
      case "$service" in
        api) key=LUMEN_API_IMAGE_REF ;;
        worker) key=LUMEN_WORKER_IMAGE_REF ;;
        agent-runtime) key=LUMEN_AGENT_RUNTIME_IMAGE_REF ;;
        web) key=LUMEN_WEB_IMAGE_REF ;;
        tgbot) key=LUMEN_TGBOT_IMAGE_REF ;;
        *) return 1 ;;
      esac
      [ -n "$env_ref" ] || return 1
      set_env_value "$SHARED_ENV" "$key" "$env_ref" || return 1
    done <<< "$rows"
  fi
  echo "::lumen-info:: phase=containers key=image_tag value=$tag"
  echo "::lumen-info:: phase=containers key=version value=$version"
}
compose_command() {
  local id="$1"
  shift
  local dir compose_file override
  dir="$(release_dir "$id")"
  compose_file="$(release_compose_path "$id")" || return 1
  override="$dir/.update-images.override.yml"
  [ -f "$override" ] || return 1
  (
    cd "$dir" || exit 1
    COMPOSE_FILE="$compose_file:$override" \
      COMPOSE_PROJECT_NAME="${LUMEN_COMPOSE_PROJECT:-lumen}" \
      docker compose "$@"
  )
}
compose_available() {
  command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}
apply_compose_release() {
  local id="$1"
  release_has_compose "$id" || return 0
  if ! compose_available; then
    echo "docker compose is unavailable for release $id" >&2
    return 1
  fi
  if release_agent_runtime_expected "$id"; then
    compose_command "$id" up -d --wait agent-runtime api worker web || return 1
  else
    compose_command "$id" up -d --wait api worker web || return 1
    docker stop lumen-agent-runtime >/dev/null 2>&1 || true
  fi
  if release_tgbot_expected; then
    compose_command "$id" --profile tgbot up -d --wait tgbot || return 1
  else
    compose_command "$id" --profile tgbot stop tgbot >/dev/null 2>&1 || return 1
  fi
}
restart_release_services() {
  command -v systemctl >/dev/null 2>&1 || return 1
  local unit
  for unit in lumen-worker.service lumen-web.service lumen-tgbot.service; do
    if ! systemctl restart "$unit"; then
      echo "restart $unit failed" >&2
      return 1
    fi
  done
  if ! systemctl --no-block restart lumen-api.service; then
    echo "restart lumen-api.service failed" >&2
    return 1
  fi
}
http_wait() {
  local url="$1"
  local attempts="${LUMEN_ROLLBACK_VERIFY_ATTEMPTS:-30}"
  local attempt=1
  command -v curl >/dev/null 2>&1 || return 1
  case "$attempts" in
    ''|*[!0-9]*) attempts=30 ;;
  esac
  [ "$attempts" -gt 0 ] || attempts=1
  while [ "$attempt" -le "$attempts" ]; do
    if curl --noproxy '*' -fsS --max-time 5 -o /dev/null "$url" 2>/dev/null; then
      return 0
    fi
    sleep 1
    attempt=$((attempt + 1))
  done
  return 1
}
verify_env_identity() {
  local id="$1"
  local dir tag version rows service image_id env_ref digest key actual
  dir="$(release_dir "$id")"
  [ -f "$SHARED_ENV" ] || return 1
  tag="$(release_line "$dir/.image-tag")" || return 1
  version="$(release_line "$dir/VERSION")" || return 1
  [ "$(env_value LUMEN_IMAGE_TAG "$SHARED_ENV")" = "$tag" ] || return 1
  [ "$(env_value LUMEN_VERSION "$SHARED_ENV")" = "$version" ] || return 1
  if release_has_compose "$id"; then
    same_path "$dir/.env" "$SHARED_ENV" || return 1
    rows="$(release_identity_rows "$id")" || return 1
    while IFS=$'\t' read -r service image_id env_ref digest; do
      case "$service" in
        api) key=LUMEN_API_IMAGE_REF ;;
        worker) key=LUMEN_WORKER_IMAGE_REF ;;
        agent-runtime) key=LUMEN_AGENT_RUNTIME_IMAGE_REF ;;
        web) key=LUMEN_WEB_IMAGE_REF ;;
        tgbot) key=LUMEN_TGBOT_IMAGE_REF ;;
        *) return 1 ;;
      esac
      actual="$(env_value "$key" "$SHARED_ENV")"
      [ -n "$env_ref" ] && [ "$actual" = "$env_ref" ] || return 1
    done <<< "$rows"
  fi
}
verify_compose_runtime() {
  local id="$1"
  local rows service image_id env_ref digest cid health actual_image repo_digests
  local tgbot_expected=0
  if release_tgbot_expected; then
    tgbot_expected=1
  fi
  compose_available || return 1
  rows="$(release_identity_rows "$id")" || return 1
  while IFS=$'\t' read -r service image_id env_ref digest; do
    [ -n "$service" ] || continue
    if [ "$service" = "tgbot" ] && [ "$tgbot_expected" -ne 1 ]; then
      continue
    fi
    cid="$(compose_command "$id" ps --status running --quiet "$service" 2>/dev/null | head -n1)"
    [ -n "$cid" ] || return 1
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid" 2>/dev/null)" || return 1
    [ "$health" = "healthy" ] || return 1
    actual_image="$(docker inspect --format '{{.Image}}' "$cid" 2>/dev/null)" || return 1
    [ "$actual_image" = "$image_id" ] || return 1
    [ "$(docker image inspect --format '{{.Id}}' "$image_id" 2>/dev/null)" = "$image_id" ] || return 1
    if [ -n "$digest" ]; then
      repo_digests="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image_id" 2>/dev/null)" || return 1
      printf '%s\n' "$repo_digests" | grep -Fxq "$digest" || return 1
    fi
    echo "::lumen-info:: phase=runtime_verify key=${service}_image value=$image_id"
  done <<< "$rows"
  if [ "$tgbot_expected" -ne 1 ]; then
    cid="$(compose_command "$id" --profile tgbot ps --status running --quiet tgbot 2>/dev/null | head -n1)"
    [ -z "$cid" ] || return 1
    echo "::lumen-info:: phase=runtime_verify key=tgbot value=not_expected"
  fi
  http_wait "${LUMEN_API_READY_URL:-http://127.0.0.1:8000/readyz}" || return 1
  http_wait "${LUMEN_WEB_HEALTH_URL:-http://127.0.0.1:3000/healthz}" || return 1
}
verify_systemd_runtime() {
  local unit
  command -v systemctl >/dev/null 2>&1 || return 1
  for unit in lumen-api.service lumen-worker.service lumen-web.service lumen-tgbot.service; do
    systemctl is-active --quiet "$unit" || return 1
  done
  http_wait "${LUMEN_API_READY_URL:-http://127.0.0.1:8000/readyz}" || return 1
  http_wait "${LUMEN_WEB_HEALTH_URL:-http://127.0.0.1:3000/healthz}" || return 1
}
verify_runtime() {
  local id="$1"
  local current_id
  current_id="$(link_release_id "$ROOT/current" 2>/dev/null)" || return 1
  [ "$current_id" = "$id" ] || return 1
  verify_env_identity "$id" || return 1
  if release_has_compose "$id"; then
    verify_compose_runtime "$id" || return 1
  else
    verify_systemd_runtime || return 1
  fi
  echo "::lumen-info:: phase=runtime_verify key=release_id value=$id"
}
switch_to_target() {
  atomic_symlink "$ROOT/current" "releases/$TARGET" || return 1
  CURRENT_SWITCHED=1
  atomic_symlink "$ROOT/previous" "releases/$ORIGINAL_ID" || return 1
}
restore_original_links() {
  [ "$ORIGINAL_RESTORABLE" -eq 1 ] || return 1
  atomic_symlink "$ROOT/current" "releases/$ORIGINAL_ID" || return 1
  if [ "$PREVIOUS_PRESENT" -eq 1 ]; then
    atomic_symlink "$ROOT/previous" "releases/$PREVIOUS_ID" || return 1
  else
    rm -f "$ROOT/previous" || return 1
  fi
  echo "::lumen-info:: phase=rollback_recovery key=current value=$ORIGINAL_ID"
}
restore_original_env() {
  if [ "$ORIGINAL_ENV_PRESENT" -eq 1 ]; then
    cp -p "$ENV_BACKUP" "$SHARED_ENV" || return 1
  fi
  apply_release_env "$ORIGINAL_ID"
}

ROLLBACK_STARTED_AT="$(date +%s)"
ROLLBACK_START="$(ts)"
SHARED_ENV="$ROOT/shared/.env"
ENV_BACKUP="$ROOT/.rollback-env.$$"
CURRENT_SWITCHED=0
ORIGINAL_ENV_PRESENT=0
ORIGINAL_RESTORABLE=0
ORIGINAL_PREFLIGHT_OK=0
PREVIOUS_PRESENT=0
ORIGINAL_ID=""
PREVIOUS_ID=""

echo "::lumen-step:: phase=rollback status=start ts=$ROLLBACK_START"
echo "::lumen-info:: phase=rollback key=target value=$TARGET"

if ORIGINAL_ID="$(link_release_id "$ROOT/current" 2>/dev/null)"; then
  ORIGINAL_RESTORABLE=1
fi
if [ -L "$ROOT/previous" ]; then
  PREVIOUS_PRESENT=1
  if ! PREVIOUS_ID="$(link_release_id "$ROOT/previous" 2>/dev/null)"; then
    ORIGINAL_RESTORABLE=0
  fi
fi
echo "::lumen-info:: phase=rollback key=previous_current value=${ORIGINAL_ID:-unknown}"
if [ -f "$SHARED_ENV" ]; then
  if cp -p "$SHARED_ENV" "$ENV_BACKUP"; then
    ORIGINAL_ENV_PRESENT=1
  else
    ORIGINAL_RESTORABLE=0
  fi
fi

failure_phase=preflight
switch_rc=0
containers_rc=1
restart_rc=1
health_rc=1
if [ "$ORIGINAL_RESTORABLE" -eq 1 ] && preflight_release "$ORIGINAL_ID"; then
  ORIGINAL_PREFLIGHT_OK=1
fi

SWITCH_T0="$(date +%s)"
echo "::lumen-step:: phase=switch status=start ts=$(ts)"
if [ "$ORIGINAL_RESTORABLE" -ne 1 ]; then
  echo "original release layout is not safely restorable" >&2
  switch_rc=1
elif [ "$ORIGINAL_PREFLIGHT_OK" -ne 1 ]; then
  echo "original release recovery proof is incomplete" >&2
  switch_rc=1
elif ! preflight_release "$TARGET"; then
  echo "target release preflight failed: $TARGET" >&2
  switch_rc=1
elif ! switch_to_target; then
  echo "target release symlink switch failed: $TARGET" >&2
  switch_rc=1
else
  failure_phase=containers
fi
echo "::lumen-step:: phase=switch status=done rc=$switch_rc dur_ms=$(elapsed_ms "$SWITCH_T0") ts=$(ts)"

if [ "$switch_rc" -eq 0 ]; then
  CONTAINERS_T0="$(date +%s)"
  echo "::lumen-step:: phase=containers status=start ts=$(ts)"
  containers_rc=0
  if ! apply_release_env "$TARGET" || ! apply_compose_release "$TARGET"; then
    containers_rc=1
  fi
  echo "::lumen-step:: phase=containers status=done rc=$containers_rc dur_ms=$(elapsed_ms "$CONTAINERS_T0") ts=$(ts)"
fi

if [ "$switch_rc" -eq 0 ] && [ "$containers_rc" -eq 0 ]; then
  failure_phase=restart
  RESTART_T0="$(date +%s)"
  echo "::lumen-step:: phase=restart status=start ts=$(ts)"
  restart_rc=0
  if ! restart_release_services; then
    restart_rc=1
  fi
  echo "::lumen-step:: phase=restart status=done rc=$restart_rc dur_ms=$(elapsed_ms "$RESTART_T0") ts=$(ts)"
fi

if [ "$switch_rc" -eq 0 ] && [ "$containers_rc" -eq 0 ] && [ "$restart_rc" -eq 0 ]; then
  failure_phase=health_post
  HEALTH_T0="$(date +%s)"
  echo "::lumen-step:: phase=health_post status=start ts=$(ts)"
  health_rc=0
  if ! verify_runtime "$TARGET"; then
    health_rc=1
  fi
  echo "::lumen-step:: phase=health_post status=done rc=$health_rc dur_ms=$(elapsed_ms "$HEALTH_T0") ts=$(ts)"
fi

rollback_rc=0
rollback_status=target_applied
requested_operation_status=succeeded
runtime_recovery_status=not_required
if [ "$switch_rc" -ne 0 ] || [ "$containers_rc" -ne 0 ] || [ "$restart_rc" -ne 0 ] || [ "$health_rc" -ne 0 ]; then
  rollback_rc=1
  requested_operation_status=failed
  rollback_status=manual_required
  runtime_recovery_status=manual_required
  echo "::lumen-info:: phase=rollback key=failure_phase value=$failure_phase"
  echo "::lumen-step:: phase=rollback_recovery status=start ts=$(ts)"
  recovery_apply_rc=0
  if [ "$ORIGINAL_RESTORABLE" -ne 1 ] || [ "$ORIGINAL_PREFLIGHT_OK" -ne 1 ]; then
    recovery_apply_rc=1
  elif [ "$CURRENT_SWITCHED" -eq 1 ]; then
    if ! restore_original_links \
      || ! restore_original_env \
      || ! apply_compose_release "$ORIGINAL_ID" \
      || ! restart_release_services; then
      recovery_apply_rc=1
    fi
  fi
  if [ "$recovery_apply_rc" -ne 0 ]; then
    echo "rollback recovery could not restore the original runtime" >&2
  elif verify_runtime "$ORIGINAL_ID"; then
    rollback_status=failed_recovered_original
    runtime_recovery_status=original_healthy
    echo "::lumen-info:: phase=rollback_recovery key=status value=original_healthy"
  else
    rollback_status=failed_original_unhealthy
    runtime_recovery_status=original_unhealthy
    echo "original release was restored but full runtime verification failed" >&2
  fi
  echo "::lumen-step:: phase=rollback_recovery status=$rollback_status rc=1 ts=$(ts)"
fi

echo "::lumen-info:: phase=rollback key=requested_operation_status value=$requested_operation_status"
echo "::lumen-info:: phase=rollback key=runtime_recovery_status value=$runtime_recovery_status"
echo "::lumen-step:: phase=rollback status=$rollback_status rc=$rollback_rc dur_ms=$(elapsed_ms "$ROLLBACK_STARTED_AT") ts=$(ts)"

case "$rollback_status" in
  target_applied|failed_recovered_original)
    rm -f "$MARKER_PATH"
    rm -f "$ENV_BACKUP"
    ;;
  failed_original_unhealthy|manual_required)
    if ! persist_manual_marker "$rollback_status"; then
      echo "manual rollback marker could not be persisted" >&2
    fi
    if [ -f "$ENV_BACKUP" ]; then
      echo "::lumen-info:: phase=rollback key=env_backup value=$ENV_BACKUP"
    fi
    ;;
esac
exit "$rollback_rc"
"""


def build_rollback_script(
    *,
    target_id: str,
    lumen_root: Path,
    marker_path: Path,
) -> str:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}", target_id):
        raise ValueError("invalid release id")
    return (
        "\n".join(
            (
                "set -uo pipefail",
                f"ROOT={shlex.quote(str(lumen_root))}",
                f"TARGET={shlex.quote(target_id)}",
                f"MARKER_PATH={shlex.quote(str(marker_path))}",
            )
        )
        + "\n"
        + _ROLLBACK_SCRIPT_BODY
    )


__all__ = ["build_rollback_script"]
