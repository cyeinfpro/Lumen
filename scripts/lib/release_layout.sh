#!/usr/bin/env bash
# Release directory, symlink cutover, and shared-path helpers.
# Sourced by scripts/lib.sh; do not execute directly.

# release id: UTC timestamp + sha7. Lexicographic order is chronological.
lumen_release_id() {
    local sha="${1:-unknown}"
    local short
    short="$(printf '%s' "${sha}" | cut -c1-7)"
    [ -n "${short}" ] || short="unknown"
    printf '%sZ-%s-%s' "$(date -u +%Y%m%dT%H%M%S)" "${short}" "$$"
}

lumen_release_current_path() {
    local root="$1"
    local cur="${root}/current"
    [ -L "${cur}" ] || return 0
    if command -v readlink >/dev/null 2>&1; then
        local target
        target="$(readlink -f "${cur}" 2>/dev/null || true)"
        if [ -n "${target}" ]; then
            printf '%s' "${target}"
            return 0
        fi
        target="$(readlink "${cur}" 2>/dev/null || true)"
        case "${target}" in
            /*) printf '%s' "${target}" ;;
            '') ;;
            *) printf '%s/%s' "${root}" "${target}" ;;
        esac
    fi
}

lumen_release_current_id() {
    local root="$1"
    local target
    target="$(lumen_release_current_path "${root}" || true)"
    [ -n "${target}" ] || return 0
    basename "${target}"
}

lumen_mv_has_T() {
    mv --version >/dev/null 2>&1 || return 1
    return 0
}

lumen_atomic_replace_symlink() {
    local link_target="$1"
    local link_path="$2"
    local link_dir
    link_dir="$(dirname "${link_path}")"
    local link_name
    link_name="$(basename "${link_path}")"
    local tmp="${link_dir}/.${link_name}.tmp.$$"

    rm -f "${tmp}" 2>/dev/null || true
    if ! ln -s "${link_target}" "${tmp}"; then
        return 1
    fi

    if lumen_mv_has_T; then
        if mv -T "${tmp}" "${link_path}"; then
            return 0
        fi
        rm -f "${tmp}" 2>/dev/null || true
        return 1
    fi

    if command -v python3 >/dev/null 2>&1; then
        if python3 -c \
                "import os, sys; os.replace(sys.argv[1], sys.argv[2])" \
                "${tmp}" "${link_path}" 2>/dev/null; then
            return 0
        fi
    fi

    rm -f "${tmp}" 2>/dev/null || true
    ln -sfn "${link_target}" "${link_path}" 2>/dev/null || return 1
    return 0
}

lumen_release_atomic_switch() {
    local root="$1"
    local new_id="$2"
    local old_id=""
    old_id="$(lumen_release_current_id "${root}" || true)"

    if [ -z "${new_id}" ]; then
        log_error "lumen_release_atomic_switch：new_id 为空。"
        return 1
    fi
    if [ ! -d "${root}/releases/${new_id}" ]; then
        log_error "lumen_release_atomic_switch：不存在 releases/${new_id}。"
        return 1
    fi

    if ! lumen_atomic_replace_symlink "releases/${new_id}" "${root}/current"; then
        log_error "切换 ${root}/current → releases/${new_id} 失败。"
        return 1
    fi

    if [ -n "${old_id}" ] && [ "${old_id}" != "${new_id}" ] \
            && [ -d "${root}/releases/${old_id}" ]; then
        lumen_atomic_replace_symlink \
            "releases/${old_id}" "${root}/previous" 2>/dev/null || true
    fi
    return 0
}

lumen_release_link_shared() {
    local release_dir="$1"
    local shared_dir="$2"
    if [ ! -d "${release_dir}" ]; then
        log_error "lumen_release_link_shared：release 目录不存在：${release_dir}"
        return 1
    fi
    if [ ! -d "${shared_dir}" ]; then
        log_error "lumen_release_link_shared：shared 目录不存在：${shared_dir}"
        return 1
    fi

    local mapping="
web-env/.env.local|apps/web/.env.local
worker-var|apps/worker/var
web-next-cache|apps/web/.next/cache
.env|.env
"
    local line src_rel dst_rel src dst dst_parent
    while IFS= read -r line; do
        [ -n "${line}" ] || continue
        src_rel="${line%%|*}"
        dst_rel="${line#*|}"
        src="${shared_dir}/${src_rel}"
        dst="${release_dir}/${dst_rel}"
        dst_parent="$(dirname "${dst}")"

        if [ ! -e "${src}" ] && [ ! -L "${src}" ]; then
            log_warn "shared 中缺少 ${src_rel}，跳过软链 ${dst_rel}。"
            continue
        fi

        mkdir -p "${dst_parent}" 2>/dev/null || true

        if [ -e "${dst}" ] || [ -L "${dst}" ]; then
            local backup
            backup="${dst}.pre-link.$(date -u +%Y%m%d%H%M%S)"
            if ! mv "${dst}" "${backup}" 2>/dev/null; then
                lumen_safe_rm_rf "${dst}" 2>/dev/null || true
            fi
        fi

        if ! ln -s "${src}" "${dst}"; then
            log_error "无法软链 ${dst} -> ${src}"
            return 1
        fi
    done <<EOF
${mapping}
EOF
    return 0
}

lumen_release_cleanup_old() {
    local root="$1"
    local keep="${2:-5}"
    local releases_dir="${root}/releases"
    [ -d "${releases_dir}" ] || return 0

    local current_id previous_id
    current_id="$(lumen_release_current_id "${root}" || true)"
    previous_id=""
    if [ -L "${root}/previous" ]; then
        local prev_link
        prev_link="$(readlink "${root}/previous" 2>/dev/null || true)"
        if [ -n "${prev_link}" ]; then
            previous_id="$(basename "${prev_link}")"
        fi
    fi

    local all_ids=()
    local entry
    for entry in "${releases_dir}"/*; do
        [ -d "${entry}" ] || continue
        all_ids+=("$(basename "${entry}")")
    done
    if [ "${#all_ids[@]}" -le "${keep}" ]; then
        return 0
    fi

    local sorted=()
    local id
    while IFS= read -r id; do
        sorted+=("${id}")
    done < <(printf '%s\n' "${all_ids[@]}" | sort -r)

    local kept=0
    local target removed=0
    for id in "${sorted[@]}"; do
        target="${releases_dir}/${id}"
        if [ -n "${current_id}" ] && [ "${id}" = "${current_id}" ]; then
            kept=$((kept + 1))
            continue
        fi
        if [ -n "${previous_id}" ] && [ "${id}" = "${previous_id}" ]; then
            kept=$((kept + 1))
            continue
        fi
        if [ "${kept}" -lt "${keep}" ]; then
            kept=$((kept + 1))
        elif rm -rf "${target}" 2>/dev/null; then
            removed=$((removed + 1))
        fi
    done
    if [ "${removed}" -gt 0 ]; then
        log_info "release cleanup：删除 ${removed} 个旧 release，保留 ${keep} 个。"
    fi
    return 0
}
