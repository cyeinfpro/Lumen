#!/usr/bin/env bash
# Canonical phase order consumed by the updater journal and tests.

LUMEN_UPDATE_PHASES="
lock
self_update_scripts
check
preflight
backup_preflight
fetch_release
set_image_tag
pull_images
check_storage
start_infra
migrate_db
switch
restart_services
start_target_worker
start_green
shift_traffic_50
shift_traffic_100
drain_blue
stop_blue
start_blue
shift_traffic_blue
stop_green
health_check
cleanup
"

lumen_update_phase_is_known() {
    local phase="$1"
    case "
${LUMEN_UPDATE_PHASES}
" in
        *"
${phase}
"*) return 0 ;;
        *) return 1 ;;
    esac
}
