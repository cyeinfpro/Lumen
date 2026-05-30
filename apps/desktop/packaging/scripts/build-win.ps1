param(
  [switch]$Clean
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "../../../..")
Set-Location $Root

$env:NEXT_PUBLIC_LUMEN_RUNTIME = "desktop"
if (-not $env:LUMEN_BACKEND_URL) {
  $env:LUMEN_BACKEND_URL = "http://127.0.0.1:8000"
}
$BuildTarget = if ($env:LUMEN_DESKTOP_BUILD_TARGET) { $env:LUMEN_DESKTOP_BUILD_TARGET.Trim() } else { "" }
$HostTriple = ((rustc -Vv | Select-String "^host:").ToString() -split "\s+")[1]
$Triple = if ($BuildTarget) { $BuildTarget } else { $HostTriple }
$GarnetVersion = if ($env:GARNET_VERSION) { $env:GARNET_VERSION } else { "1.1.9" }
$DotnetRuntimeVersion = if ($env:DOTNET_RUNTIME_VERSION) { $env:DOTNET_RUNTIME_VERSION } else { "8.0.27" }
$CleanTauriOutputs = $Clean -or $env:LUMEN_CLEAN_TAURI_OUTPUTS -eq "1"
$NodeRuntimeVersion = if ($env:NODE_RUNTIME_VERSION) {
  $env:NODE_RUNTIME_VERSION.TrimStart("v")
} elseif (Test-Path "NODE_VERSION") {
  ((Get-Content "NODE_VERSION" -Raw).Trim()).TrimStart("v")
} else {
  (node -e "const pkg=require('./apps/web/package.json'); const raw=(pkg.engines&&pkg.engines.node)||process.versions.node; console.log(String(raw).replace(/^[^0-9]*/, '').split(/[ <>=|]/)[0]);").Trim()
}

function New-NodeRuntimeLinkOrCopy {
  param(
    [string]$Source,
    [string]$Target
  )
  if (Test-Path $Target) {
    Remove-Item -Recurse -Force $Target -ErrorAction SilentlyContinue
  }
  try {
    New-Item -ItemType SymbolicLink -Path $Target -Target $Source -ErrorAction Stop | Out-Null
  } catch {
    Copy-Item $Source $Target -Force
  }
}

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
      -Uri "https://builds.dotnet.microsoft.com/dotnet/Runtime/$DotnetRuntimeVersion/dotnet-runtime-$DotnetRuntimeVersion-$rid.zip" `
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
    if (Test-Path $binNode) {
      New-NodeRuntimeLinkOrCopy -Source $binNode -Target $rootNode
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

function Maybe-CleanTauriOutputs {
  if ($CleanTauriOutputs) {
    Clean-TauriOutputs
  }
}

function Test-TargetRunsOnHost {
  if (-not $BuildTarget) {
    return $true
  }
  if ($Triple -eq $HostTriple) {
    return $true
  }

  $targetArch = ($Triple -split "-")[0]
  $hostArch = ($HostTriple -split "-")[0]
  return $targetArch -eq $hostArch
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

function Verify-DesktopResources {
  $checks = @(
    @{ Path = "apps/desktop/resources/web/server.js"; Label = "Next standalone server" },
    @{ Path = "apps/desktop/resources/web/package.json"; Label = "Next standalone package metadata" },
    @{ Path = "apps/desktop/resources/runtime/node/node.exe"; Label = "Node runtime" },
    @{ Path = "apps/desktop/resources/runtime/lumen-api/lumen-api.exe"; Label = "API runtime" },
    @{ Path = "apps/desktop/resources/runtime/lumen-worker/lumen-worker.exe"; Label = "worker runtime" },
    @{ Path = "apps/desktop/resources/runtime/lumen-redis/lumen-redis.exe"; Label = "Redis-compatible runtime" },
    @{ Path = "apps/desktop/resources/runtime/dotnet/dotnet.exe"; Label = ".NET runtime" }
  )
  $missing = [System.Collections.Generic.List[string]]::new()
  foreach ($check in $checks) {
    if (-not (Test-Path -Path $check.Path -PathType Leaf)) {
      $missing.Add("missing bundled $($check.Label): $($check.Path)")
    }
  }
  if ($missing.Count -gt 0) {
    throw ($missing -join "`n")
  }

  $nodeRuntime = "apps/desktop/resources/runtime/node/node.exe"
  if (Test-TargetRunsOnHost) {
    & $nodeRuntime --version | Out-Null
  } else {
    Write-Host "Skipping executable Node runtime check for cross-architecture target $Triple on host $HostTriple."
  }
}

function Verify-GarnetCli {
  $garnet = "apps/desktop/resources/runtime/lumen-redis/lumen-redis.exe"
  if (-not (Test-TargetRunsOnHost)) {
    Write-Host "Skipping executable Garnet CLI check for cross-architecture target $Triple on host $HostTriple."
    return
  }
  $previousDotnetRoot = $env:DOTNET_ROOT
  $previousDotnetMultilevelLookup = $env:DOTNET_MULTILEVEL_LOOKUP
  try {
    $env:DOTNET_ROOT = Join-Path $Root "apps/desktop/resources/runtime/dotnet"
    $env:DOTNET_MULTILEVEL_LOOKUP = "0"
    $help = & $garnet --help 2>&1 | Out-String
  } finally {
    if ($null -eq $previousDotnetRoot) {
      Remove-Item Env:DOTNET_ROOT -ErrorAction SilentlyContinue
    } else {
      $env:DOTNET_ROOT = $previousDotnetRoot
    }
    if ($null -eq $previousDotnetMultilevelLookup) {
      Remove-Item Env:DOTNET_MULTILEVEL_LOOKUP -ErrorAction SilentlyContinue
    } else {
      $env:DOTNET_MULTILEVEL_LOOKUP = $previousDotnetMultilevelLookup
    }
  }
  foreach ($flag in @("--lua", "--checkpointdir", "--aof", "--recover")) {
    if (-not $help.Contains($flag)) {
      throw "bundled Garnet runtime does not advertise required flag $flag`n$help"
    }
  }
}

function Get-WindowsSignCommandConfig {
  if (-not $env:WINDOWS_SIGNING_THUMBPRINT -and -not $env:WINDOWS_SIGNING_CERT_PATH) {
    return $null
  }

  return @{
    cmd = "pwsh"
    args = @(
      "-NoProfile",
      "-NonInteractive",
      "-ExecutionPolicy",
      "Bypass",
      "-File",
      (Join-Path $PSScriptRoot "sign-win.ps1"),
      "-Path",
      "%1"
    )
  }
}

function Get-TauriConfigArgs {
  $signCommand = Get-WindowsSignCommandConfig
  if (-not $env:TAURI_UPDATER_PUBKEY -and -not $signCommand) {
    if ($env:GITHUB_REF_TYPE -eq "tag" -or ($env:GITHUB_REF -and $env:GITHUB_REF.StartsWith("refs/tags/"))) {
      throw "TAURI_UPDATER_PUBKEY is required for tagged desktop releases."
    }
    return @()
  }
  if ($env:TAURI_UPDATER_PUBKEY -and -not $env:TAURI_SIGNING_PRIVATE_KEY) {
    throw "TAURI_UPDATER_PUBKEY requires TAURI_SIGNING_PRIVATE_KEY for updater artifact signing"
  }
  $configPath = Join-Path $Root "apps/desktop/target/tauri-updater.conf.json"
  New-Item -ItemType Directory -Force (Split-Path $configPath -Parent) | Out-Null
  $bundle = @{}
  if ($env:TAURI_UPDATER_PUBKEY) {
    # Keep Windows updater artifacts in the v1-compatible NSIS zip format so the
    # release workflow can upload and publish the updater bundle it expects.
    $bundle.createUpdaterArtifacts = "v1Compatible"
  }
  if ($signCommand) {
    $bundle.windows = @{
      signCommand = $signCommand
    }
  }

  $config = @{
    bundle = $bundle
  }
  if ($env:TAURI_UPDATER_PUBKEY) {
    $config.plugins = @{
      updater = @{
        active = $true
        endpoints = @(
          if ($env:LUMEN_UPDATER_ENDPOINT) {
            $env:LUMEN_UPDATER_ENDPOINT.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
          } else {
            "https://github.com/cyeinfpro/Lumen/releases/latest/download/latest.json"
          }
        )
        pubkey = $env:TAURI_UPDATER_PUBKEY
      }
    }
  }
  $config | ConvertTo-Json -Depth 8 | Set-Content -Encoding UTF8 $configPath
  return @("--config", $configPath)
}

python scripts/version.py check
if (-not (Get-Command cargo-tauri -ErrorAction SilentlyContinue)) {
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
Maybe-CleanTauriOutputs
Prepare-StaticResourcePlaceholders

Remove-Item -Recurse -Force apps/desktop/resources/runtime/lumen-api -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force apps/desktop/resources/runtime/lumen-worker -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force apps/desktop/resources/runtime | Out-Null
Copy-Item -Recurse apps/desktop/dist/lumen-api apps/desktop/resources/runtime/lumen-api
Copy-Item -Recurse apps/desktop/dist/lumen-worker apps/desktop/resources/runtime/lumen-worker
Prepare-Garnet
Prepare-DotnetRuntime
Prepare-StaticResourcePlaceholders
Verify-DesktopResources
Verify-GarnetCli

Maybe-CleanTauriOutputs
$tauriConfigArgs = Get-TauriConfigArgs
Push-Location apps/desktop
$cargoArgs = @("tauri", "build", "--bundles", "nsis")
if ($BuildTarget) {
  $cargoArgs += @("--target", $BuildTarget)
}
$cargoArgs += $tauriConfigArgs
cargo @cargoArgs
Pop-Location

if ($env:WINDOWS_SIGNING_THUMBPRINT -or $env:WINDOWS_SIGNING_CERT_PATH) {
  $bundleRoot = if ($BuildTarget) {
    Join-Path $Root "apps/desktop/target/$BuildTarget/release/bundle/nsis"
  } else {
    Join-Path $Root "apps/desktop/target/release/bundle/nsis"
  }
  if (Test-Path $bundleRoot) {
    $signArgs = @("-Path", $bundleRoot)
    if ($env:WINDOWS_SIGNING_THUMBPRINT) {
      $signArgs += @("-Thumbprint", $env:WINDOWS_SIGNING_THUMBPRINT)
    }
    if ($env:WINDOWS_SIGNING_CERT_PATH) {
      $signArgs += @("-CertPath", $env:WINDOWS_SIGNING_CERT_PATH)
    }
    if ($env:WINDOWS_SIGNING_CERT_PASSWORD) {
      $signArgs += @("-CertPassword", $env:WINDOWS_SIGNING_CERT_PASSWORD)
    }
    & (Join-Path $PSScriptRoot "sign-win.ps1") @signArgs
  }
}
