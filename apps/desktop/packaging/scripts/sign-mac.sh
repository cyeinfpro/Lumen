#!/usr/bin/env bash
set -euo pipefail

APP_PATH="${1:?usage: sign-mac.sh /path/to/Lumen.app}"
IDENTITY="${APPLE_CODESIGN_IDENTITY:?APPLE_CODESIGN_IDENTITY is required}"
ENTITLEMENTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lumen.entitlements"

find "$APP_PATH" -type f \( -perm -111 -o -name "*.dylib" -o -name "*.so" \) -print0 |
  while IFS= read -r -d '' file; do
    codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" \
      --sign "$IDENTITY" "$file"
  done

codesign --force --deep --options runtime --timestamp --entitlements "$ENTITLEMENTS" \
  --sign "$IDENTITY" "$APP_PATH"
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
