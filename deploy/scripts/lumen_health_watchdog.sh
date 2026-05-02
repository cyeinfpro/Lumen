#!/usr/bin/env bash
# Probe local Lumen services and restart the small edge services when they stop
# responding while systemd still considers them active.
set -euo pipefail

API_SERVICE="${LUMEN_API_SERVICE:-lumen-api}"
WEB_SERVICE="${LUMEN_WEB_SERVICE:-lumen-web}"
API_URL="${LUMEN_API_WATCHDOG_URL:-http://127.0.0.1:8000/healthz}"
WEB_URL="${LUMEN_WEB_WATCHDOG_URL:-http://127.0.0.1:3000/}"
ATTEMPTS="${LUMEN_WATCHDOG_ATTEMPTS:-3}"
CONNECT_TIMEOUT="${LUMEN_WATCHDOG_CONNECT_TIMEOUT:-1}"
MAX_TIME="${LUMEN_WATCHDOG_MAX_TIME:-3}"

log() {
  printf '[lumen-watchdog] %s\n' "$*" >&2
}

is_active() {
  systemctl is-active --quiet "$1"
}

probe() {
  local url="$1"
  local _attempt
  for _attempt in $(seq 1 "$ATTEMPTS"); do
    if curl -fsS \
      --connect-timeout "$CONNECT_TIMEOUT" \
      --max-time "$MAX_TIME" \
      -o /dev/null \
      "$url"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

dump_api_state() {
  local pid
  pid="$(systemctl show -p MainPID --value "$API_SERVICE" 2>/dev/null || true)"
  log "api_state=$(systemctl is-active "$API_SERVICE" 2>/dev/null || true) api_pid=${pid:-unknown}"
  if [[ -n "${pid:-}" && "$pid" != "0" ]]; then
    log "api_children:"
    pgrep -P "$pid" -a 2>/dev/null || true
    log "api_sockets:"
    ss -tnp 2>/dev/null | grep "pid=$pid" | head -40 || true
  fi
}

restart_api() {
  log "api probe failed url=$API_URL attempts=$ATTEMPTS; requesting stack dump"
  dump_api_state
  systemctl kill -s SIGUSR1 --kill-who=all "$API_SERVICE" 2>/dev/null || true
  sleep 1
  log "restarting $API_SERVICE"
  systemctl restart "$API_SERVICE"
}

restart_web() {
  log "web probe failed url=$WEB_URL attempts=$ATTEMPTS; restarting $WEB_SERVICE"
  systemctl restart "$WEB_SERVICE"
}

if is_active "$API_SERVICE"; then
  probe "$API_URL" || restart_api
else
  log "$API_SERVICE is not active; skipping api probe"
fi

if is_active "$WEB_SERVICE"; then
  probe "$WEB_URL" || restart_web
else
  log "$WEB_SERVICE is not active; skipping web probe"
fi
