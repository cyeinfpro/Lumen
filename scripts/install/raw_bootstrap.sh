#!/usr/bin/env bash
# Raw install checkout, legacy-state recovery, and atomic publish dispatch.

raw_materialize_source() {
    local repo_url="$1" destination="$2"
    if [ "${RAW_BOOTSTRAP_SOURCE_MODE}" = "rolling" ]; then
        git clone --quiet --depth 1 --branch "${RAW_BOOTSTRAP_SOURCE_REF}" \
            "${repo_url}" "${destination}"
        return
    fi
    mkdir -p "${destination}"
    git init --quiet "${destination}"
    git -C "${destination}" remote add origin "${repo_url}"
    git -C "${destination}" fetch --quiet --depth 1 origin \
        "refs/tags/${RAW_BOOTSTRAP_SOURCE_TAG}:refs/tags/${RAW_BOOTSTRAP_SOURCE_TAG}"
    local fetched=""
    fetched="$(
        git -C "${destination}" rev-parse --verify \
            "refs/tags/${RAW_BOOTSTRAP_SOURCE_TAG}^{commit}"
    )" || return 1
    if [ "${fetched}" != "${RAW_BOOTSTRAP_SOURCE_COMMIT}" ]; then
        printf '[ERROR] release tag 与 manifest commit 不一致：tag=%s git=%s manifest=%s\n' \
            "${RAW_BOOTSTRAP_SOURCE_TAG}" "${fetched}" \
            "${RAW_BOOTSTRAP_SOURCE_COMMIT}" >&2
        return 1
    fi
    git -C "${destination}" checkout --quiet --detach \
        "${RAW_BOOTSTRAP_SOURCE_COMMIT}"
    git -C "${destination}" reset --hard --quiet \
        "${RAW_BOOTSTRAP_SOURCE_COMMIT}"
}

raw_update_existing_checkout() {
    local install_dir="$1"
    if [ "${RAW_BOOTSTRAP_SOURCE_MODE}" = "rolling" ]; then
        git -C "${install_dir}" fetch --quiet origin \
            "${RAW_BOOTSTRAP_SOURCE_REF}"
        git -C "${install_dir}" checkout --quiet \
            "${RAW_BOOTSTRAP_SOURCE_REF}"
        git -C "${install_dir}" reset --hard \
            "origin/${RAW_BOOTSTRAP_SOURCE_REF}"
        return
    fi
    git -C "${install_dir}" fetch --quiet --depth 1 origin \
        "refs/tags/${RAW_BOOTSTRAP_SOURCE_TAG}:refs/tags/${RAW_BOOTSTRAP_SOURCE_TAG}"
    local fetched=""
    fetched="$(
        git -C "${install_dir}" rev-parse --verify \
            "refs/tags/${RAW_BOOTSTRAP_SOURCE_TAG}^{commit}"
    )" || return 1
    [ "${fetched}" = "${RAW_BOOTSTRAP_SOURCE_COMMIT}" ] || return 1
    git -C "${install_dir}" checkout --quiet --detach \
        "${RAW_BOOTSTRAP_SOURCE_COMMIT}"
    git -C "${install_dir}" reset --hard \
        "${RAW_BOOTSTRAP_SOURCE_COMMIT}"
}

raw_recover_bootstrap_transactions() {
    local install_dir="$1"
    local parent="" base="" transaction="" helper="" current=""
    parent="$(cd "$(dirname "${install_dir}")" 2>/dev/null && pwd -P)" \
        || return 0
    base="$(basename "${install_dir}")"
    for transaction in "${parent}/.${base}.bootstrap."*; do
        [ -d "${transaction}" ] || continue
        helper="${transaction}/bootstrap_transaction.py"
        [ -f "${helper}" ] && [ ! -L "${helper}" ] || {
            printf '[ERROR] bootstrap transaction 缺少安全恢复 helper：%s\n' \
                "${transaction}" >&2
            return 1
        }
        printf '[WARN] 检测到未完成 bootstrap transaction，先恢复：%s\n' \
            "${transaction}" >&2
        python3 "${helper}" recover "${transaction}" || return 1
    done
    [ -L "${install_dir}/current" ] || return 0
    current="$(cd "${install_dir}/current" 2>/dev/null && pwd -P)" || return 0
    for transaction in "${current}/.scripts.bootstrap."*; do
        [ -d "${transaction}" ] || continue
        helper="${transaction}/bootstrap_transaction.py"
        [ -f "${helper}" ] && [ ! -L "${helper}" ] || return 1
        python3 "${helper}" recover "${transaction}" || return 1
    done
}

detect_install_state() {
    local directory="$1"
    if [ ! -e "${directory}" ]; then
        printf 'empty'
    elif [ -d "${directory}/.git" ]; then
        printf 'git'
    elif [ -L "${directory}/current" ] \
            || { [ -d "${directory}/releases" ] \
                && [ -d "${directory}/shared" ]; }; then
        printf 'release'
    elif [ -f "${directory}/scripts/lib.sh" ] \
            || [ -d "${directory}/apps/api" ] \
            || [ -d "${directory}/packages/core" ]; then
        printf 'inplace'
    elif [ ! -d "${directory}" ] \
            || find "${directory}" -mindepth 1 -maxdepth 1 \
                -print -quit 2>/dev/null | grep -q .; then
        printf 'mixed'
    elif [ -r "${directory}" ] && [ -x "${directory}" ]; then
        printf 'empty'
    else
        printf 'mixed'
    fi
}

overlay_release_scripts() {
    local repo_url="$1" release_dir="$2"
    shift 2
    local tmp_dir="" real_release="" transaction_helper=""
    tmp_dir="$(mktemp -d)" || return 1
    # shellcheck disable=SC2064
    trap "rm -rf '${tmp_dir}'" RETURN
    raw_materialize_source "${repo_url}" "${tmp_dir}/repo" || return 1
    if ! real_release="$(cd "${release_dir}" 2>/dev/null && pwd -P)" \
            || [ ! -d "${real_release}/scripts" ]; then
        printf '[ERROR] 当前 release scripts 目录无效：%s\n' "${release_dir}" >&2
        return 1
    fi
    transaction_helper="${tmp_dir}/repo/scripts/install/bootstrap_transaction.py"
    [ -f "${transaction_helper}" ] && [ ! -L "${transaction_helper}" ] || {
        printf '[ERROR] 远端仓库缺少安全 bootstrap transaction helper。\n' >&2
        return 1
    }
    exec python3 "${transaction_helper}" publish \
        "${tmp_dir}/repo/scripts" "${real_release}/scripts" "${tmp_dir}" \
        "${RAW_INSTALL_FROM_STDIN:-0}" \
        -- bash "${real_release}/scripts/install.sh" "$@"
}

publish_inplace_repository() {
    local repo_url="$1" install_dir="$2"
    shift 2
    local tmp_dir="" transaction_helper=""
    tmp_dir="$(mktemp -d)" || return 1
    # shellcheck disable=SC2064
    trap "rm -rf '${tmp_dir}'" RETURN
    raw_materialize_source "${repo_url}" "${tmp_dir}/repo" || return 1
    transaction_helper="${tmp_dir}/repo/scripts/install/bootstrap_transaction.py"
    [ -f "${transaction_helper}" ] && [ ! -L "${transaction_helper}" ] || {
        printf '[ERROR] 远端仓库缺少安全 bootstrap transaction helper。\n' >&2
        return 1
    }
    export LUMEN_BOOTSTRAP_MODE="update"
    exec python3 "${transaction_helper}" publish-inplace \
        "${tmp_dir}/repo" "${install_dir}" "${tmp_dir}" \
        "${RAW_INSTALL_FROM_STDIN:-0}" \
        -- bash "${install_dir}/scripts/install.sh" "$@"
}

bootstrap_from_raw_script() {
    local repo_url="$1"
    shift
    local default_dir="" install_dir="" state="" script_path="" backup=""
    local args=("$@")
    # No explicit action keeps the interactive menu instead of skipping
    # straight into an automatic update.
    # 避免脚本一运行就跳过菜单。
    [ "${#args[@]}" -gt 0 ] || args=("menu")
    if [ "${EUID:-$(id -u)}" = "0" ]; then
        default_dir="${LUMEN_DEPLOY_ROOT:-/opt/lumen}"
    else
        default_dir="${HOME:-$PWD}/Lumen"
    fi
    install_dir="${LUMEN_INSTALL_DIR:-${default_dir}}"
    if raw_have_cmd python3; then
        raw_recover_bootstrap_transactions "${install_dir}" || return 1
    fi
    state="$(detect_install_state "${install_dir}")"
    printf '[INFO] bootstrap source：mode=%s ref=%s commit=%s state=%s\n' \
        "${RAW_BOOTSTRAP_SOURCE_MODE}" "${RAW_BOOTSTRAP_SOURCE_REF}" \
        "${RAW_BOOTSTRAP_SOURCE_COMMIT:-rolling}" "${state}"

    case "${state}" in
        git)
            raw_update_existing_checkout "${install_dir}"
            export LUMEN_BOOTSTRAP_MODE="auto"
            ;;
        release)
            if [ -L "${install_dir}/current" ]; then
                export LUMEN_BOOTSTRAP_MODE="update"
                overlay_release_scripts \
                    "${repo_url}" "${install_dir}/current" "${args[@]}"
            fi
            export LUMEN_BOOTSTRAP_MODE="update"
            ;;
        inplace)
            publish_inplace_repository \
                "${repo_url}" "${install_dir}" "${args[@]}"
            ;;
        mixed)
            backup="${install_dir}.bak.$(date -u +%Y%m%d%H%M%S 2>/dev/null || date +%s)"
            mv "${install_dir}" "${backup}"
            raw_materialize_source "${repo_url}" "${install_dir}"
            export LUMEN_BOOTSTRAP_MODE="install"
            ;;
        empty|*)
            raw_materialize_source "${repo_url}" "${install_dir}"
            export LUMEN_BOOTSTRAP_MODE="install"
            ;;
    esac

    if [ "${state}" = "release" ] && [ -L "${install_dir}/current" ] \
            && [ -f "${install_dir}/current/scripts/install.sh" ]; then
        script_path="${install_dir}/current/scripts/install.sh"
    elif [ -f "${install_dir}/scripts/install.sh" ]; then
        script_path="${install_dir}/scripts/install.sh"
    elif [ -f "${install_dir}/current/scripts/install.sh" ]; then
        script_path="${install_dir}/current/scripts/install.sh"
    else
        printf '[ERROR] bootstrap 后找不到 install.sh：%s\n' "${install_dir}" >&2
        return 1
    fi
    raw_drain_bootstrap_stdin
    if [ -r /dev/tty ] && ( : </dev/tty ) 2>/dev/null; then
        exec bash "${script_path}" "${args[@]}" </dev/tty
    fi
    exec bash "${script_path}" "${args[@]}"
}
