#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$ROOT"

export NEXT_PUBLIC_LUMEN_RUNTIME=desktop
export LUMEN_BACKEND_URL="${LUMEN_BACKEND_URL:-http://127.0.0.1:8000}"
TRIPLE="$(rustc -Vv | awk '/^host:/ {print $2}')"
GARNET_VERSION="${GARNET_VERSION:-1.1.9}"
DOTNET_RUNTIME_VERSION="${DOTNET_RUNTIME_VERSION:-8.0.27}"
NODE_RUNTIME_VERSION="${NODE_RUNTIME_VERSION:-$(node -p 'process.versions.node')}"

if command -v brew >/dev/null 2>&1; then
  LIBPQ_PREFIX="$(brew --prefix libpq 2>/dev/null || true)"
  if [ -n "$LIBPQ_PREFIX" ]; then
    export PATH="$LIBPQ_PREFIX/bin:$PATH"
    export LDFLAGS="-L$LIBPQ_PREFIX/lib ${LDFLAGS:-}"
    export CPPFLAGS="-I$LIBPQ_PREFIX/include ${CPPFLAGS:-}"
    export PKG_CONFIG_PATH="$LIBPQ_PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
    export LIBRARY_PATH="$LIBPQ_PREFIX/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"
  fi
  OPENSSL_PREFIX="$(brew --prefix openssl@3 2>/dev/null || true)"
  if [ -n "$OPENSSL_PREFIX" ]; then
    export PATH="$OPENSSL_PREFIX/bin:$PATH"
    export LDFLAGS="-L$OPENSSL_PREFIX/lib ${LDFLAGS:-}"
    export CPPFLAGS="-I$OPENSSL_PREFIX/include ${CPPFLAGS:-}"
    export PKG_CONFIG_PATH="$OPENSSL_PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
    export LIBRARY_PATH="$OPENSSL_PREFIX/lib${LIBRARY_PATH:+:$LIBRARY_PATH}"
  fi
fi

prepare_garnet() {
  local dest="apps/desktop/resources/runtime/lumen-redis"
  rm -rf "$dest"
  mkdir -p "$dest"
  if [ -n "${GARNET_BIN:-}" ]; then
    local source_bin="$GARNET_BIN"
    local source_dir
    source_dir="$(cd "$(dirname "$source_bin")" && pwd)"
    cp -R "$source_dir"/. "$dest"/
    cp "$source_bin" "$dest/lumen-redis"
    chmod +x "$dest/lumen-redis"
    return
  fi

  local asset
  case "$(uname -m)" in
    arm64|aarch64) asset="osx-arm64-based.tar.xz" ;;
    x86_64|amd64) asset="osx-x64-based.tar.xz" ;;
    *) echo "unsupported macOS architecture for Garnet: $(uname -m)" >&2; exit 1 ;;
  esac
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  curl -fsSL \
    "https://github.com/microsoft/garnet/releases/download/v${GARNET_VERSION}/${asset}" \
    -o "$tmp/garnet.tar.xz"
  tar -xf "$tmp/garnet.tar.xz" -C "$tmp"
  cp -R "$tmp/net8.0"/. "$dest"/
  mv "$dest/GarnetServer" "$dest/lumen-redis"
  chmod +x "$dest/lumen-redis"
}

prepare_dotnet_runtime() {
  local dest="apps/desktop/resources/runtime/dotnet"
  rm -rf "$dest"
  mkdir -p "$dest"
  if [ -n "${DOTNET_RUNTIME_DIR:-}" ]; then
    cp -R "$DOTNET_RUNTIME_DIR"/. "$dest"/
    chmod +x "$dest/dotnet" 2>/dev/null || true
    return
  fi

  local rid
  case "$(uname -m)" in
    arm64|aarch64) rid="osx-arm64" ;;
    x86_64|amd64) rid="osx-x64" ;;
    *) echo "unsupported macOS architecture for .NET runtime: $(uname -m)" >&2; exit 1 ;;
  esac
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  curl -fsSL \
    "https://dotnetcli.azureedge.net/dotnet/Runtime/${DOTNET_RUNTIME_VERSION}/dotnet-runtime-${DOTNET_RUNTIME_VERSION}-${rid}.tar.gz" \
    -o "$tmp/dotnet-runtime.tar.gz"
  tar -xzf "$tmp/dotnet-runtime.tar.gz" -C "$dest"
  chmod +x "$dest/dotnet" 2>/dev/null || true
}

prepare_node_runtime() {
  local dest="apps/desktop/resources/runtime/node"
  rm -rf "$dest"
  mkdir -p "$dest"
  if [ -n "${NODE_RUNTIME_DIR:-}" ]; then
    cp -R "$NODE_RUNTIME_DIR"/. "$dest"/
    if [ -x "$dest/bin/node" ] && [ ! -e "$dest/node" ]; then
      ln -s bin/node "$dest/node"
    fi
    chmod +x "$dest/node" "$dest/bin/node" 2>/dev/null || true
    return
  fi

  local arch
  case "$(uname -m)" in
    arm64|aarch64) arch="arm64" ;;
    x86_64|amd64) arch="x64" ;;
    *) echo "unsupported macOS architecture for Node: $(uname -m)" >&2; exit 1 ;;
  esac
  local version="${NODE_RUNTIME_VERSION#v}"
  local asset="node-v${version}-darwin-${arch}.tar.xz"
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  curl -fsSL \
    "https://nodejs.org/dist/v${version}/${asset}" \
    -o "$tmp/node.tar.xz"
  tar -xf "$tmp/node.tar.xz" -C "$tmp"
  cp -R "$tmp/node-v${version}-darwin-${arch}"/. "$dest"/
  ln -sf bin/node "$dest/node"
  chmod +x "$dest/node" "$dest/bin/node" 2>/dev/null || true
}

clean_tauri_outputs() {
  local target_dir="apps/desktop/target"
  for profile in release debug; do
    local profile_dir="$target_dir/$profile"
    if [ -d "$profile_dir/resources" ]; then
      chmod -R u+w "$profile_dir/resources" 2>/dev/null || true
      rm -rf "$profile_dir/resources"
    fi
    rm -f \
      "$profile_dir/lumen-desktop" \
      "$profile_dir/Lumen" \
      "$profile_dir/lumen-web" \
      "$profile_dir/lumen-api" \
      "$profile_dir/lumen-worker" \
      "$profile_dir/lumen-redis"
  done
}

prepare_static_resource_placeholders() {
  local path
  for path in \
    "apps/desktop/resources/alembic/desktop/.placeholder" \
    "apps/desktop/resources/licenses/.placeholder"; do
    mkdir -p "$(dirname "$path")"
    : > "$path"
  done
}

prepare_tauri_config_args() {
  TAURI_CONFIG_ARGS=()
  TAURI_CONFIG_ARGS_COUNT=0
  if [ -z "${TAURI_UPDATER_PUBKEY:-}" ]; then
    return
  fi
  if [ -z "${TAURI_SIGNING_PRIVATE_KEY:-}" ]; then
    echo "TAURI_UPDATER_PUBKEY requires TAURI_SIGNING_PRIVATE_KEY for updater artifact signing" >&2
    exit 1
  fi
  local config_path="apps/desktop/target/tauri-updater.conf.json"
  mkdir -p "$(dirname "$config_path")"
  python3 - <<'PY'
import json
import os
from pathlib import Path

config = {
    "bundle": {"createUpdaterArtifacts": True},
    "plugins": {"updater": {"pubkey": os.environ["TAURI_UPDATER_PUBKEY"]}},
}
Path("apps/desktop/target/tauri-updater.conf.json").write_text(
    json.dumps(config, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
PY
  TAURI_CONFIG_ARGS=(--config "$config_path")
  TAURI_CONFIG_ARGS_COUNT=${#TAURI_CONFIG_ARGS[@]}
}

prepare_macos_signing_env() {
  if [ -n "${APPLE_SIGNING_IDENTITY+x}" ] && [ -z "${APPLE_SIGNING_IDENTITY//[[:space:]]/}" ]; then
    unset APPLE_SIGNING_IDENTITY
  fi
  if [ -z "${APPLE_SIGNING_IDENTITY:-}" ]; then
    export APPLE_SIGNING_IDENTITY="-"
    echo "APPLE_SIGNING_IDENTITY is not configured; using ad-hoc bundle signing." >&2
  fi
}

verify_macos_bundle_signature() {
  local app_path="apps/desktop/target/release/bundle/macos/Lumen.app"
  if [ ! -d "$app_path" ]; then
    echo "missing macOS app bundle: $app_path" >&2
    exit 1
  fi
  codesign --verify --deep --strict --verbose=2 "$app_path"
}

python3 scripts/version.py check
if ! cargo tauri --version >/dev/null 2>&1; then
  cargo install tauri-cli --locked
fi

(
  cd apps/web
  npm ci
  npm run build:desktop
)

rm -rf apps/desktop/dist/web
mkdir -p apps/desktop/dist/web
cp apps/desktop/packaging/startup/index.html apps/desktop/dist/web/index.html

rm -rf apps/desktop/resources/web
mkdir -p apps/desktop/resources/web
cp -R apps/web/.next/standalone/. apps/desktop/resources/web/
mkdir -p apps/desktop/resources/web/.next
cp -R apps/web/.next/static apps/desktop/resources/web/.next/static
if [ -d apps/web/public ]; then
  cp -R apps/web/public apps/desktop/resources/web/public
fi
prepare_node_runtime

uv sync --all-packages
uv run --with "pyinstaller>=6,<7" pyinstaller --clean --noconfirm --distpath apps/desktop/dist \
  apps/desktop/packaging/pyinstaller/lumen-api.spec
uv run --with "pyinstaller>=6,<7" pyinstaller --clean --noconfirm --distpath apps/desktop/dist \
  apps/desktop/packaging/pyinstaller/lumen-worker.spec
clean_tauri_outputs
mkdir -p apps/desktop/binaries
: > "apps/desktop/binaries/lumen-web-${TRIPLE}"
chmod +x "apps/desktop/binaries/lumen-web-${TRIPLE}"
prepare_static_resource_placeholders
(
  cd apps/desktop
  cargo build --release --bin lumen-web
)

rm -rf apps/desktop/resources/runtime/lumen-api apps/desktop/resources/runtime/lumen-worker
mkdir -p apps/desktop/resources/runtime
cp -R apps/desktop/dist/lumen-api apps/desktop/resources/runtime/lumen-api
cp -R apps/desktop/dist/lumen-worker apps/desktop/resources/runtime/lumen-worker
prepare_garnet
prepare_dotnet_runtime
prepare_static_resource_placeholders

cp -f apps/desktop/target/release/lumen-web "apps/desktop/binaries/lumen-web-${TRIPLE}"
chmod +x "apps/desktop/binaries/lumen-web-${TRIPLE}"

clean_tauri_outputs
prepare_tauri_config_args
prepare_macos_signing_env
(
  cd apps/desktop
  if [ "${TAURI_CONFIG_ARGS_COUNT:-0}" -gt 0 ]; then
    cargo tauri build --bundles dmg "${TAURI_CONFIG_ARGS[@]}"
  else
    cargo tauri build --bundles dmg
  fi
)
verify_macos_bundle_signature
