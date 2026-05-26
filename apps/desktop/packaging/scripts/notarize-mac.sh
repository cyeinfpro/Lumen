#!/usr/bin/env bash
set -euo pipefail

DMG_PATH="${1:?usage: notarize-mac.sh /path/to/Lumen.dmg}"

xcrun notarytool submit "$DMG_PATH" \
  --apple-id "${APPLE_ID:?APPLE_ID is required}" \
  --team-id "${APPLE_TEAM_ID:?APPLE_TEAM_ID is required}" \
  --password "${APPLE_APP_PASSWORD:?APPLE_APP_PASSWORD is required}" \
  --wait
xcrun stapler staple "$DMG_PATH"
xcrun stapler validate "$DMG_PATH"
