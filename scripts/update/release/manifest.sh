#!/usr/bin/env bash
# Release manifest, immutable source, and version binding helpers.

sed_replacement_escape() {
    printf '%s' "$1" | sed 's/[\/&#]/\\&/g'
}

semver_from_image_tag() {
    local tag="${1:-}"
    case "${tag}" in
        v[0-9]*.[0-9]*.[0-9]*)
            printf '%s\n' "${tag#v}"
            return 0
            ;;
    esac
    return 1
}

release_version_for_target() {
    local release_dir="${1:-}"
    local target_tag="${2:-}"
    local version=""
    if tag_version="$(semver_from_image_tag "${target_tag}" 2>/dev/null || true)" \
            && [ -n "${tag_version}" ]; then
        printf '%s\n' "${tag_version}"
        return 0
    fi
    if [ -n "${release_dir}" ] && [ -f "${release_dir}/VERSION" ]; then
        version="$(head -n1 "${release_dir}/VERSION" 2>/dev/null | tr -d '[:space:]')"
    fi
    if printf '%s\n' "${version}" | grep -Eq '^[0-9]+[.][0-9]+[.][0-9]+(-[0-9A-Za-z.-]+)?$'; then
        printf '%s\n' "${version}"
        return 0
    fi
    return 1
}

release_commit_is_valid() {
    printf '%s\n' "${1:-}" | grep -Eq '^[0-9a-f]{40}$'
}

release_manifest_commit_for_tag() {
    local manifest_file="${1:-}"
    local release_tag="${2:-}"
    [ -f "${manifest_file}" ] || return 1
    python3 - "${manifest_file}" "${release_tag}" <<'PY'
import json
import re
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    raise SystemExit(1)

if not isinstance(payload, dict):
    raise SystemExit(1)
commit = payload.get("commit_sha")
if payload.get("schema_version") != 1 or payload.get("version") != sys.argv[2]:
    raise SystemExit(1)
if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
    raise SystemExit(1)
print(commit)
PY
}

release_manifest_immutable_image_for_service() {
    local manifest_file="${1:-}"
    local release_tag="${2:-}"
    local service="${3:-}"
    [ -f "${manifest_file}" ] || return 1
    python3 - "${manifest_file}" "${release_tag}" "${service}" <<'PY'
import json
import re
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    raise SystemExit(1)

tag = sys.argv[2]
service = sys.argv[3]
images = payload.get("images")
image = images.get(service) if isinstance(images, dict) else None
immutable_ref = image.get("immutable_ref") if isinstance(image, dict) else None
expected_prefix = f"ghcr.io/cyeinfpro/lumen-{service}@sha256:"
if payload.get("schema_version") != 1 or payload.get("version") != tag:
    raise SystemExit(1)
if not isinstance(immutable_ref, str) or not immutable_ref.startswith(expected_prefix):
    raise SystemExit(1)
if not re.fullmatch(r"ghcr\.io/cyeinfpro/lumen-[a-z]+@sha256:[0-9a-f]{64}", immutable_ref):
    raise SystemExit(1)
print(immutable_ref)
PY
}

prepare_official_release_source_manifest() {
    local release_tag="${1:-}"
    local output="${2:-}"
    local commit_sha=""
    local api_image=""
    if ! lumen_fetch_release_manifest "${release_tag}" "${output}"; then
        log_error "[fetch_release] 无法取得 ${release_tag} 的官方 release manifest。"
        return 1
    fi
    commit_sha="$(
        release_manifest_commit_for_tag "${output}" "${release_tag}" 2>/dev/null \
            || true
    )"
    api_image="$(
        release_manifest_immutable_image_for_service \
            "${output}" "${release_tag}" api 2>/dev/null || true
    )"
    if ! release_commit_is_valid "${commit_sha}" || [ -z "${api_image}" ]; then
        log_error "[fetch_release] ${release_tag} manifest 缺少有效源码 commit 或 API immutable image。"
        return 1
    fi
    RELEASE_EXPECTED_COMMIT="${commit_sha}"
    RELEASE_SOURCE_API_IMAGE="${api_image}"
    emit_info fetch_release expected_source_commit "${commit_sha}"
    emit_info fetch_release source_image "${api_image}"
    return 0
}

discard_release_source_manifest_cache() {
    if [ -n "${RELEASE_SOURCE_MANIFEST_CACHE:-}" ]; then
        rm -f "${RELEASE_SOURCE_MANIFEST_CACHE}" 2>/dev/null || true
    fi
    RELEASE_SOURCE_MANIFEST_CACHE=""
}

record_release_source_commit() {
    local candidate="${1:-}"
    local proof="${2:-unknown}"
    if ! release_commit_is_valid "${candidate}"; then
        return 1
    fi
    if [ -n "${RELEASE_SOURCE_COMMIT:-}" ] \
            && [ "${RELEASE_SOURCE_COMMIT}" != "${candidate}" ]; then
        log_error "[fetch_release] 源码 commit 证明冲突：${RELEASE_SOURCE_COMMIT} (${RELEASE_SOURCE_COMMIT_PROOF:-unknown}) != ${candidate} (${proof})"
        return 1
    fi
    if [ -n "${RELEASE_EXPECTED_COMMIT:-}" ] \
            && [ "${RELEASE_EXPECTED_COMMIT}" != "${candidate}" ]; then
        log_error "[fetch_release] 源码 commit 与官方 release manifest 不一致：source=${candidate} expected=${RELEASE_EXPECTED_COMMIT} proof=${proof}"
        return 1
    fi
    RELEASE_SOURCE_COMMIT="${candidate}"
    if [ -z "${RELEASE_SOURCE_COMMIT_PROOF:-}" ]; then
        RELEASE_SOURCE_COMMIT_PROOF="${proof}"
    fi
    return 0
}

release_image_revision_commit() {
    local image="${1:-}"
    local revision=""
    [ -n "${image}" ] || return 1
    revision="$(docker image inspect \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
        "${image}" 2>/dev/null | head -n1 | tr -d '[:space:]' || true)"
    release_commit_is_valid "${revision}" || return 1
    printf '%s\n' "${revision}"
}

verify_release_source_manifest_binding() {
    local manifest_file="${1:-}"
    local release_tag="${2:-}"
    local manifest_commit=""
    manifest_commit="$(release_manifest_commit_for_tag \
        "${manifest_file}" "${release_tag}" 2>/dev/null || true)"
    if ! release_commit_is_valid "${manifest_commit}"; then
        log_error "[set_image_tag] ${release_tag} manifest 缺少有效的 40 位 commit_sha。"
        return 1
    fi
    if ! release_commit_is_valid "${RELEASE_SOURCE_COMMIT:-}"; then
        log_error "[set_image_tag] 无法证明待发布源码对应 ${release_tag} 的 40 位 commit；拒绝混用未绑定源码与正式 release 镜像。"
        return 1
    fi
    if [ "${RELEASE_SOURCE_COMMIT}" != "${manifest_commit}" ]; then
        log_error "[set_image_tag] 源码 commit 与 release manifest 不一致：source=${RELEASE_SOURCE_COMMIT} manifest=${manifest_commit} tag=${release_tag}"
        return 1
    fi
    log_info "[set_image_tag] 源码 commit 已绑定 release manifest：${manifest_commit} (${RELEASE_SOURCE_COMMIT_PROOF:-unknown})"
    return 0
}
