#!/usr/bin/env bash
# Compatibility wrapper retained for older release layouts.
set -euo pipefail
UPDATE_MODULE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${UPDATE_MODULE_DIR}/runner.sh" "$@"
