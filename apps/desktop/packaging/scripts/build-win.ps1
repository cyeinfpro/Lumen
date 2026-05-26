$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "../../../..")
Set-Location $Root

$env:NEXT_PUBLIC_LUMEN_RUNTIME = "desktop"
if (-not $env:LUMEN_BACKEND_URL) {
  $env:LUMEN_BACKEND_URL = "http://127.0.0.1:8000"
}
$Triple = ((rustc -Vv | Select-String "^host:").ToString() -split "\s+")[1]
$GarnetVersion = if ($env:GARNET_VERSION) { $env:GARNET_VERSION } else { "1.1.9" }
$DotnetRuntimeVersion = if ($env:DOTNET_RUNTIME_VERSION) { $env:DOTNET_RUNTIME_VERSION } else { "8.0.27" }
$NodeRuntimeVersion = if ($env:NODE_RUNTIME_VERSION) { $env:NODE_RUNTIME_VERSION.TrimStart("v") } else { (node -p "process.versions.node").Trim() }

function Prepare-Garnet {
  $dest = Join-Path $Root "apps/desktop/resources/runtime/lumen-redis"
  Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force $dest | Out-Null

  if ($env:GARNET_BIN) {
    $sourceBin = Resolve-Path $env:GARNET_BIN
    $sourceDir = Split-Path $sourceBin -Parent
    Copy-Item -Recurse (Join-Path $sourceDir "*") $dest
    Copy-Item $sourceBin (Join-Path $dest "lumen-redis.exe")
    return
  }

  $asset = if ($Triple -like "aarch64-*") {
    "win-arm64-based-readytorun.zip"
  } else {
    "win-x64-based-readytorun.zip"
  }
  $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("lumen-garnet-" + [System.Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Force $tmp | Out-Null
  try {
    $archive = Join-Path $tmp "garnet.zip"
    Invoke-WebRequest `
      -Uri "https://github.com/microsoft/garnet/releases/download/v$GarnetVersion/$asset" `
      -OutFile $archive
    Expand-Archive -Path $archive -DestinationPath $tmp -Force
    Copy-Item -Recurse (Join-Path $tmp "net8.0/*") $dest
    Move-Item (Join-Path $dest "GarnetServer.exe") (Join-Path $dest "lumen-redis.exe") -Force
  } finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  }
}

function Prepare-DotnetRuntime {
  $dest = Join-Path $Root "apps/desktop/resources/runtime/dotnet"
  Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force $dest | Out-Null

  if ($env:DOTNET_RUNTIME_DIR) {
    Copy-Item -Recurse (Join-Path (Resolve-Path $env:DOTNET_RUNTIME_DIR) "*") $dest
    return
  }

  $rid = if ($Triple -like "aarch64-*") { "win-arm64" } else { "win-x64" }
  $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("lumen-dotnet-" + [System.Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Force $tmp | Out-Null
  try {
    $archive = Join-Path $tmp "dotnet-runtime.zip"
    Invoke-WebRequest `
      -Uri "https://dotnetcli.azureedge.net/dotnet/Runtime/$DotnetRuntimeVersion/dotnet-runtime-$DotnetRuntimeVersion-$rid.zip" `
      -OutFile $archive
    Expand-Archive -Path $archive -DestinationPath $dest -Force
  } finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  }
}

function Prepare-NodeRuntime {
  $dest = Join-Path $Root "apps/desktop/resources/runtime/node"
  Remove-Item -Recurse -Force $dest -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force $dest | Out-Null

  if ($env:NODE_RUNTIME_DIR) {
    Copy-Item -Recurse (Join-Path (Resolve-Path $env:NODE_RUNTIME_DIR) "*") $dest
    $binNode = Join-Path $dest "bin/node.exe"
    $rootNode = Join-Path $dest "node.exe"
    if ((Test-Path $binNode) -and -not (Test-Path $rootNode)) {
      Copy-Item $binNode $rootNode
    }
    return
  }

  $arch = if ($Triple -like "aarch64-*") { "arm64" } else { "x64" }
  $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("lumen-node-" + [System.Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Force $tmp | Out-Null
  try {
    $asset = "node-v$NodeRuntimeVersion-win-$arch.zip"
    $archive = Join-Path $tmp "node.zip"
    Invoke-WebRequest `
      -Uri "https://nodejs.org/dist/v$NodeRuntimeVersion/$asset" `
      -OutFile $archive
    Expand-Archive -Path $archive -DestinationPath $tmp -Force
    Copy-Item -Recurse (Join-Path $tmp "node-v$NodeRuntimeVersion-win-$arch/*") $dest
  } finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  }
}

function Clean-TauriOutputs {
  $targetDir = Join-Path $Root "apps/desktop/target"
  foreach ($profile in @("release", "debug")) {
    $profileDir = Join-Path $targetDir $profile
    $resourcesDir = Join-Path $profileDir "resources"
    Remove-Item -Recurse -Force $resourcesDir -ErrorAction SilentlyContinue
    foreach ($name in @("lumen-desktop.exe", "Lumen.exe", "lumen-web.exe", "lumen-api.exe", "lumen-worker.exe", "lumen-redis.exe")) {
      Remove-Item -Force (Join-Path $profileDir $name) -ErrorAction SilentlyContinue
    }
  }
}

function Prepare-StaticResourcePlaceholders {
  foreach ($path in @(
    "apps/desktop/resources/alembic/desktop/.placeholder",
    "apps/desktop/resources/licenses/.placeholder"
  )) {
    New-Item -ItemType Directory -Force (Split-Path $path -Parent) | Out-Null
    if (-not (Test-Path $path)) {
      Set-Content -Path $path -Value "desktop resource placeholder" -Encoding UTF8
    }
  }
}

function Get-TauriConfigArgs {
  if (-not $env:TAURI_UPDATER_PUBKEY) {
    return @()
  }
  if (-not $env:TAURI_SIGNING_PRIVATE_KEY) {
    throw "TAURI_UPDATER_PUBKEY requires TAURI_SIGNING_PRIVATE_KEY for updater artifact signing"
  }
  $configPath = Join-Path $Root "apps/desktop/target/tauri-updater.conf.json"
  New-Item -ItemType Directory -Force (Split-Path $configPath -Parent) | Out-Null
  @{
    bundle = @{
      createUpdaterArtifacts = $true
    }
    plugins = @{
      updater = @{
        pubkey = $env:TAURI_UPDATER_PUBKEY
      }
    }
  } | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $configPath
  return @("--config", $configPath)
}

python scripts/version.py check
try {
  cargo tauri --version | Out-Null
} catch {
  cargo install tauri-cli --locked
}

Push-Location apps/web
npm ci
npm run build:desktop
Pop-Location

Remove-Item -Recurse -Force apps/desktop/dist/web -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force apps/desktop/dist/web | Out-Null
Copy-Item apps/desktop/packaging/startup/index.html apps/desktop/dist/web/index.html

Remove-Item -Recurse -Force apps/desktop/resources/web -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force apps/desktop/resources/web | Out-Null
Copy-Item -Recurse apps/web/.next/standalone/* apps/desktop/resources/web/
New-Item -ItemType Directory -Force apps/desktop/resources/web/.next | Out-Null
Copy-Item -Recurse apps/web/.next/static apps/desktop/resources/web/.next/static
if (Test-Path apps/web/public) {
  Copy-Item -Recurse apps/web/public apps/desktop/resources/web/public
}
Prepare-NodeRuntime

uv sync --all-packages
uv run --with "pyinstaller>=6,<7" pyinstaller --clean --noconfirm --distpath apps/desktop/dist apps/desktop/packaging/pyinstaller/lumen-api.spec
uv run --with "pyinstaller>=6,<7" pyinstaller --clean --noconfirm --distpath apps/desktop/dist apps/desktop/packaging/pyinstaller/lumen-worker.spec
Clean-TauriOutputs
Push-Location apps/desktop
cargo build --release --bin lumen-web
Pop-Location

Remove-Item -Recurse -Force apps/desktop/resources/runtime/lumen-api -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force apps/desktop/resources/runtime/lumen-worker -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force apps/desktop/resources/runtime | Out-Null
Copy-Item -Recurse apps/desktop/dist/lumen-api apps/desktop/resources/runtime/lumen-api
Copy-Item -Recurse apps/desktop/dist/lumen-worker apps/desktop/resources/runtime/lumen-worker
Prepare-Garnet
Prepare-DotnetRuntime
Prepare-StaticResourcePlaceholders

New-Item -ItemType Directory -Force apps/desktop/binaries | Out-Null
Copy-Item apps/desktop/target/release/lumen-web.exe "apps/desktop/binaries/lumen-web-$Triple.exe"

Clean-TauriOutputs
$tauriConfigArgs = Get-TauriConfigArgs
Push-Location apps/desktop
cargo tauri build --bundles nsis @tauriConfigArgs
Pop-Location
