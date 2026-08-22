#!/usr/bin/env bash
# Immutable image binding and startup proof for Lumen update targets.
LUMEN_UPDATE_IMAGE_PROOF_NAME=".update-image-proof.json"
LUMEN_UPDATE_IMAGE_OVERRIDE_NAME=".update-images.override.yml"
# Once an image binding exists, every compose command against this release
# consumes the same Image ID override. Commands against an old rollback release
# keep their original compose files because the resolved directory differs.
lumen_compose_in() {
    local dir="$1" target_dir="" compose_files="${COMPOSE_FILE:-}"
    shift
    if [ -n "${TARGET_IMAGE_OVERRIDE_FILE:-}" ] \
            && [ -d "${NEW_RELEASE:-}" ] && [ -d "${dir}" ]; then
        target_dir="$(cd "${NEW_RELEASE}" && pwd -P)"
        if [ "$(cd "${dir}" && pwd -P)" = "${target_dir}" ]; then
            case ":${compose_files}:" in
                *":${TARGET_IMAGE_OVERRIDE_FILE}:"*) ;;
                *)
                    compose_files="${target_dir}/docker-compose.yml:${TARGET_IMAGE_OVERRIDE_FILE}"
                    ;;
            esac
        fi
    fi
    if [ -n "${compose_files}" ]; then
        (
            cd "${dir}"
            export COMPOSE_FILE="${compose_files}"
            lumen_compose "$@"
        )
    else
        ( cd "${dir}" && lumen_compose "$@" )
    fi
}
lumen_update_tgbot_enabled() {
    env_key_present "${SHARED_ENV:?}" "TELEGRAM_BOT_TOKEN"
}
lumen_update_required_image_references() {
    local images extra="" service expected key
    local core_services=(api worker web)
    if ! images="$(
        lumen_compose_in "${NEW_RELEASE:?}" config --images 2>/dev/null
    )"; then
        log_error "[pull_images] 无法枚举 compose 镜像。"
        return 1
    fi
    if lumen_update_tgbot_enabled; then
        if [ "${TGBOT_IMAGE_READY:-0}" != "1" ]; then
            log_error "[pull_images] tgbot 已启用但镜像未准备好，拒绝生成不完整 proof。"
            return 1
        fi
        if ! extra="$(
            lumen_compose_in "${NEW_RELEASE}" \
                --profile tgbot config --images 2>/dev/null
        )"; then
            log_error "[pull_images] 无法枚举已启用的 tgbot 镜像。"
            return 1
        fi
    fi
    images="$(printf '%s\n%s\n' "${images}" "${extra}" | sed '/^$/d')"
    if [ -f "${NEW_RELEASE}/docker-compose.yml" ] \
            && grep -Eq '^[[:space:]]{2}agent-runtime:[[:space:]]*$' \
                "${NEW_RELEASE}/docker-compose.yml"; then
        core_services=(api worker agent-runtime web)
    fi
    for service in "${core_services[@]}"; do
        case "${service}" in
            api) key="LUMEN_API_IMAGE_REF" ;;
            worker) key="LUMEN_WORKER_IMAGE_REF" ;;
            agent-runtime) key="LUMEN_AGENT_RUNTIME_IMAGE_REF" ;;
            web) key="LUMEN_WEB_IMAGE_REF" ;;
        esac
        eval "expected=\${${key}:-}"
        if ! lumen_image_ref_is_immutable "${expected}"; then
            expected="${LUMEN_IMAGE_REGISTRY%/}/lumen-${service}:${TARGET_TAG:?}"
        fi
        if ! printf '%s\n' "${images}" | grep -Fxq "${expected}"; then
            log_error "[pull_images] compose 未声明预期镜像：${expected}"
            return 1
        fi
        printf '%s\t%s\n' "${service}" "${expected}"
    done
    if lumen_update_tgbot_enabled; then
        expected="${LUMEN_TGBOT_IMAGE_REF:-}"
        if ! lumen_image_ref_is_immutable "${expected}"; then
            expected="${LUMEN_IMAGE_REGISTRY%/}/lumen-tgbot:${TARGET_TAG:?}"
        fi
        if ! printf '%s\n' "${images}" | grep -Fxq "${expected}"; then
            log_error "[pull_images] compose 未声明已启用的 tgbot 镜像：${expected}"
            return 1
        fi
        printf 'tgbot\t%s\n' "${expected}"
    fi
}
lumen_update_manifest_immutable_ref() {
    local service="$1"
    local guard="${LUMEN_RELEASE_MANIFEST_GUARD:-${SCRIPT_DIR}/release_manifest_guard.py}"
    [ -n "${RELEASE_MANIFEST_FILE:-}" ] || return 0
    python3 "${guard}" entries \
        --manifest "${RELEASE_MANIFEST_FILE}" \
        --tag "${RELEASE_MANIFEST_TAG}" \
        --service "${service}" \
        | awk -F '\t' 'NR == 1 { print $4 }'
}
lumen_update_capture_image_record() {
    local service="$1" image="$2" records_file="$3"
    local expected_immutable="$4" inspect_file
    inspect_file="$(mktemp "${NEW_RELEASE}/.image-inspect.XXXXXXXXXX")" || return 1
    if ! lumen_docker image inspect "${image}" > "${inspect_file}" 2>/dev/null; then
        rm -f "${inspect_file}"
        log_error "[pull_images] 无法 inspect 镜像：${image}"
        return 1
    fi
    if ! python3 - \
            "${inspect_file}" "${records_file}" "${service}" "${image}" \
            "${RELEASE_SOURCE_COMMIT:?}" "${LUMEN_UPDATE_BUILD:-0}" \
            "${expected_immutable}" <<'PY'
import json
from pathlib import Path
import re
import sys
inspect_path = Path(sys.argv[1])
records_path = Path(sys.argv[2])
service, source_ref, source_commit = sys.argv[3:6]
build = sys.argv[6] == "1"
expected_immutable = sys.argv[7]
image_id_re = re.compile(r"^sha256:[0-9a-f]{64}$")
commit_re = re.compile(r"^[0-9a-f]{40}$")
digest_ref_re = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
payload = json.loads(inspect_path.read_text(encoding="utf-8"))
if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
    raise SystemExit(f"invalid docker inspect payload for {source_ref}")
image = payload[0]
image_id = image.get("Id")
if not isinstance(image_id, str) or not image_id_re.fullmatch(image_id):
    raise SystemExit(f"invalid Image ID for {source_ref}")
config = image.get("Config")
labels = config.get("Labels") if isinstance(config, dict) else None
revision = labels.get("org.opencontainers.image.revision") if isinstance(labels, dict) else None
revision = revision.strip() if isinstance(revision, str) else ""
if not build and (not commit_re.fullmatch(revision) or revision != source_commit):
    raise SystemExit(
        "rolling image/source commit mismatch: "
        f"image={source_ref} image_id={image_id} "
        f"revision={revision or '<missing>'} source={source_commit}"
    )
repository = source_ref.split("@", 1)[0]
last_slash = repository.rfind("/")
last_colon = repository.rfind(":")
if last_colon > last_slash:
    repository = repository[:last_colon]
repo_digests = image.get("RepoDigests")
matching = sorted(
    {
        value
        for value in repo_digests or []
        if isinstance(value, str)
        and digest_ref_re.fullmatch(value)
        and value.startswith(repository + "@")
    }
)
if not build and not matching:
    raise SystemExit(f"missing immutable RepoDigest for {source_ref} on {image_id}")
if expected_immutable and expected_immutable not in matching:
    raise SystemExit(
        "release manifest digest mismatch on inspected Image ID: "
        f"service={service} image_id={image_id} expected={expected_immutable}"
    )
record = {
    "image_id": image_id,
    "repo_digests": matching,
    "revision": revision or None,
    "service": service,
    "source_ref": source_ref,
}
with records_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
PY
    then
        rm -f "${inspect_file}"
        log_error "[pull_images] 镜像不可变 proof 校验失败：${image}"
        return 1
    fi
    rm -f "${inspect_file}"
}

lumen_update_render_image_binding() {
    local records_file="$1" proof_file="$2" override_file="$3"
    python3 - \
        "${records_file}" "${proof_file}" "${override_file}" \
        "${TARGET_TAG:?}" "${RELEASE_SOURCE_COMMIT:?}" \
        "${LUMEN_UPDATE_BUILD:-0}" <<'PY'
import json
from pathlib import Path
import sys

records_path, proof_path, override_path = map(Path, sys.argv[1:4])
target_tag, source_commit = sys.argv[4:6]
build = sys.argv[6] == "1"
records = [
    json.loads(line)
    for line in records_path.read_text(encoding="utf-8").splitlines()
    if line
]
services = {record["service"]: record for record in records}
expected = {"api", "worker", "web"}
allowed = expected | {"agent-runtime", "tgbot"}
if not expected.issubset(services) or set(services) - allowed:
    raise SystemExit("immutable image proof service set is incomplete")

compose_services = {
    "api": services["api"]["image_id"],
    "api-green": services["api"]["image_id"],
    "bootstrap": services["api"]["image_id"],
    "migrate": services["api"]["image_id"],
    "web": services["web"]["image_id"],
    "worker": services["worker"]["image_id"],
}
if "agent-runtime" in services:
    compose_services["agent-runtime"] = services["agent-runtime"]["image_id"]
if "tgbot" in services:
    compose_services["tgbot"] = services["tgbot"]["image_id"]
proof = {
    "build": build,
    "compose_services": compose_services,
    "schema": 1,
    "services": services,
    "source_commit": source_commit,
    "target_tag": target_tag,
}
proof_path.write_text(
    json.dumps(proof, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
lines = ["services:"]
for service, image_id in sorted(compose_services.items()):
    lines.extend(
        (
            f"  {service}:",
            f'    image: "{image_id}"',
            "    pull_policy: never",
        )
    )
override_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

lumen_update_bind_immutable_images() {
    local records_tmp proof_tmp override_tmp references
    local service image expected_immutable
    if [ -n "${TARGET_IMAGE_SET_DIGEST:-}" ]; then
        if ! lumen_update_activate_bound_image_override \
                || ! lumen_update_journal_bind_target; then
            log_error "[pull_images] 已绑定 immutable image proof 无法重放。"
            return 1
        fi
        emit_info pull_images image_set_digest "${TARGET_IMAGE_SET_DIGEST}"
        return 0
    fi
    records_tmp="$(mktemp "${NEW_RELEASE}/.image-records.XXXXXXXXXX")" || return 1
    proof_tmp="$(mktemp "${NEW_RELEASE}/.image-proof.XXXXXXXXXX")" || {
        rm -f "${records_tmp}"
        return 1
    }
    override_tmp="$(mktemp "${NEW_RELEASE}/.image-override.XXXXXXXXXX")" || {
        rm -f "${records_tmp}" "${proof_tmp}"
        return 1
    }
    if ! references="$(lumen_update_required_image_references)"; then
        rm -f "${records_tmp}" "${proof_tmp}" "${override_tmp}"
        return 1
    fi
    while IFS=$'\t' read -r service image; do
        [ -n "${service}" ] || continue
        expected_immutable=""
        if [ -n "${RELEASE_MANIFEST_FILE:-}" ]; then
            if ! expected_immutable="$(
                lumen_update_manifest_immutable_ref "${service}"
            )" || [ -z "${expected_immutable}" ]; then
                rm -f "${records_tmp}" "${proof_tmp}" "${override_tmp}"
                log_error "[pull_images] 无法读取 ${service} 的 manifest immutable ref。"
                return 1
            fi
        fi
        if ! lumen_update_capture_image_record \
                "${service}" "${image}" "${records_tmp}" "${expected_immutable}"; then
            rm -f "${records_tmp}" "${proof_tmp}" "${override_tmp}"
            return 1
        fi
    done <<< "${references}"
    if [ ! -s "${records_tmp}" ] \
            || ! lumen_update_render_image_binding \
                "${records_tmp}" "${proof_tmp}" "${override_tmp}"; then
        rm -f "${records_tmp}" "${proof_tmp}" "${override_tmp}"
        return 1
    fi

    TARGET_IMAGE_PROOF_FILE="${NEW_RELEASE}/${LUMEN_UPDATE_IMAGE_PROOF_NAME}"
    TARGET_IMAGE_OVERRIDE_FILE="${NEW_RELEASE}/${LUMEN_UPDATE_IMAGE_OVERRIDE_NAME}"
    if ! lumen_update_copy_file_durable "${proof_tmp}" "${TARGET_IMAGE_PROOF_FILE}" \
            || ! lumen_update_copy_file_durable \
                "${override_tmp}" "${TARGET_IMAGE_OVERRIDE_FILE}"; then
        rm -f "${records_tmp}" "${proof_tmp}" "${override_tmp}"
        log_error "[pull_images] 无法持久化 immutable image proof。"
        return 1
    fi
    rm -f "${records_tmp}" "${proof_tmp}" "${override_tmp}"
    TARGET_IMAGE_PROOF_SHA256="$(
        lumen_update_file_sha256 "${TARGET_IMAGE_PROOF_FILE}"
    )"
    TARGET_IMAGE_OVERRIDE_SHA256="$(
        lumen_update_file_sha256 "${TARGET_IMAGE_OVERRIDE_FILE}"
    )"
    TARGET_IMAGE_SET_DIGEST="sha256:${TARGET_IMAGE_PROOF_SHA256}"
    TARGET_ROLLING_DIGEST=""
    if lumen_image_tag_is_rolling "${TARGET_TAG}"; then
        TARGET_ROLLING_DIGEST="${TARGET_IMAGE_SET_DIGEST}"
    fi
    export TARGET_IMAGE_PROOF_FILE TARGET_IMAGE_PROOF_SHA256
    export TARGET_IMAGE_OVERRIDE_FILE TARGET_IMAGE_OVERRIDE_SHA256
    export TARGET_IMAGE_SET_DIGEST TARGET_ROLLING_DIGEST TGBOT_IMAGE_READY
    if ! lumen_update_journal_bind_target; then
        log_error "[pull_images] immutable image proof 与已绑定 target 冲突。"
        return 1
    fi
    emit_info pull_images image_set_digest "${TARGET_IMAGE_SET_DIGEST}"
}

lumen_update_validate_bound_image_artifacts() {
    [ -n "${TARGET_IMAGE_PROOF_FILE:-}" ] \
        && [ -n "${TARGET_IMAGE_PROOF_SHA256:-}" ] \
        && [ -n "${TARGET_IMAGE_OVERRIDE_FILE:-}" ] \
        && [ -n "${TARGET_IMAGE_OVERRIDE_SHA256:-}" ] \
        && [ -n "${TARGET_IMAGE_SET_DIGEST:-}" ] \
        || return 1
    [ "$(lumen_update_file_sha256 "${TARGET_IMAGE_PROOF_FILE}" 2>/dev/null)" \
        = "${TARGET_IMAGE_PROOF_SHA256}" ] \
        && [ "$(lumen_update_file_sha256 "${TARGET_IMAGE_OVERRIDE_FILE}" 2>/dev/null)" \
        = "${TARGET_IMAGE_OVERRIDE_SHA256}" ] \
        && [ "${TARGET_IMAGE_SET_DIGEST}" \
        = "sha256:${TARGET_IMAGE_PROOF_SHA256}" ]
}

lumen_update_bound_image_set_digest() {
    lumen_update_validate_bound_image_artifacts || return 1
    printf '%s\n' "${TARGET_IMAGE_SET_DIGEST}"
}

lumen_update_activate_bound_image_override() {
    local service image_id actual records
    lumen_update_validate_bound_image_artifacts || return 1
    if ! records="$(
        python3 - "${TARGET_IMAGE_PROOF_FILE}" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for service, image_id in sorted(payload["compose_services"].items()):
    print(f"{service}\t{image_id}")
PY
    )"; then
        return 1
    fi
    while IFS=$'\t' read -r service image_id; do
        [ -n "${service}" ] || continue
        actual="$(
            lumen_docker image inspect --format '{{.Id}}' \
                "${image_id}" 2>/dev/null || true
        )"
        if [ "${actual}" != "${image_id}" ]; then
            log_error "[restart_services] bound Image ID 不可用：service=${service} expected=${image_id} actual=${actual:-<missing>}"
            return 1
        fi
    done <<< "${records}"
}

lumen_update_expected_service_image_id() {
    python3 - "${TARGET_IMAGE_PROOF_FILE:?}" "$1" <<'PY'
import json
from pathlib import Path
import sys

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = payload.get("compose_services", {}).get(sys.argv[2])
if not isinstance(value, str):
    raise SystemExit(1)
print(value)
PY
}

lumen_update_verify_running_service_image() {
    local service="$1" container="$2" expected actual
    expected="$(lumen_update_expected_service_image_id "${service}")" || return 1
    actual="$(
        lumen_docker inspect --format '{{.Image}}' "${container}" 2>/dev/null || true
    )"
    if [ "${actual}" != "${expected}" ]; then
        log_error "[restart_services] 运行镜像 proof 失败：service=${service} expected=${expected} actual=${actual:-<missing>}"
        return 1
    fi
}
