#!/usr/bin/env bash
# Stable compatibility entrypoint for the modular update state machine.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/update/runner.sh" "$@"
