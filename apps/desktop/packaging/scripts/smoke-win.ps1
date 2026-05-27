param(
  [string]$Executable = ""
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "../../../..")
if (-not $Executable) {
  $Executable = Join-Path $Root "apps/desktop/target/release/lumen-desktop.exe"
}
$Executable = (Resolve-Path $Executable).Path
if (-not (Test-Path $Executable)) {
  throw "missing executable: $Executable"
}

$work = Join-Path ([System.IO.Path]::GetTempPath()) ("lumen-desktop-smoke-" + [System.Guid]::NewGuid().ToString("N"))
$smokeHome = Join-Path $work "home"
$localAppData = Join-Path $work "LocalAppData"
$roamingAppData = Join-Path $work "AppDataRoaming"
$dataRoot = Join-Path $work "data-root"
$stdoutPath = Join-Path $work "app.stdout.log"
$stderrPath = Join-Path $work "app.stderr.log"
New-Item -ItemType Directory -Force $smokeHome, $localAppData, $roamingAppData, $dataRoot | Out-Null
$appProcess = $null
$httpTimeoutSec = 8

function Stop-ProcessTree {
  param([int]$ProcessId)
  $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
  foreach ($child in $children) {
    Stop-ProcessTree -ProcessId ([int]$child.ProcessId)
  }
  Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

function Get-ProcessTreeIds {
  param([int]$ProcessId)
  $ids = New-Object System.Collections.Generic.List[int]
  $ids.Add($ProcessId)
  $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
  foreach ($child in $children) {
    foreach ($id in (Get-ProcessTreeIds -ProcessId ([int]$child.ProcessId))) {
      $ids.Add($id)
    }
  }
  return $ids
}

function Get-ListeningProcessIds {
  param([int]$Port)
  try {
    return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
      Select-Object -ExpandProperty OwningProcess -Unique)
  } catch {
    return @()
  }
}

function Get-HttpStatus {
  param([string]$Uri)
  try {
    $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec $httpTimeoutSec -Uri $Uri
    return [int]$response.StatusCode
  } catch {
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
      return [int]$_.Exception.Response.StatusCode
    }
    if ($_.Exception.StatusCode) {
      return [int]$_.Exception.StatusCode
    }
    throw
  }
}

function Get-HttpStatusNoRedirect {
  param([string]$Uri)
  $request = [System.Net.HttpWebRequest][System.Net.WebRequest]::Create($Uri)
  $request.AllowAutoRedirect = $false
  $request.Timeout = $httpTimeoutSec * 1000
  try {
    $response = $request.GetResponse()
    try {
      return [int]$response.StatusCode
    } finally {
      $response.Close()
    }
  } catch [System.Net.WebException] {
    if ($_.Exception.Response) {
      $response = $_.Exception.Response
      try {
        return [int]$response.StatusCode
      } finally {
        $response.Close()
      }
    }
    throw
  }
}

function Invoke-JsonRequest {
  param(
    [string]$Method = "GET",
    [string]$Uri,
    [object]$Body = $null,
    [hashtable]$Headers = @{}
  )
  $requestHeaders = @{ Accept = "application/json" }
  foreach ($key in $Headers.Keys) {
    $requestHeaders[$key] = $Headers[$key]
  }
  $params = @{
    UseBasicParsing = $true
    TimeoutSec = $httpTimeoutSec
    Uri = $Uri
    Method = $Method
    Headers = $requestHeaders
  }
  if ($null -ne $Body) {
    $params["ContentType"] = "application/json"
    $params["Body"] = ($Body | ConvertTo-Json -Depth 8 -Compress)
  }
  try {
    $response = Invoke-WebRequest @params
    $json = $null
    $hashJson = $null
    if (-not [string]::IsNullOrWhiteSpace([string]$response.Content)) {
      $json = $response.Content | ConvertFrom-Json -NoEnumerate
      $hashJson = $response.Content | ConvertFrom-Json -AsHashtable -NoEnumerate
    }
    return [pscustomobject]@{
      StatusCode = [int]$response.StatusCode
      Json = $json
      HashJson = $hashJson
      Content = [string]$response.Content
    }
  } catch {
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
      return [pscustomobject]@{
        StatusCode = [int]$_.Exception.Response.StatusCode
        Json = $null
        HashJson = $null
        Content = ""
      }
    }
    if ($_.Exception.StatusCode) {
      return [pscustomobject]@{
        StatusCode = [int]$_.Exception.StatusCode
        Json = $null
        HashJson = $null
        Content = ""
      }
    }
    throw
  }
}

function Get-JsonArrayProperty {
  param(
    [object]$Json,
    [object]$HashJson = $null,
    [string]$Name
  )
  $value = $null
  if ($null -ne $HashJson -and $HashJson -is [System.Collections.IDictionary] -and $HashJson.Contains($Name)) {
    $value = $HashJson[$Name]
  } elseif ($null -ne $Json) {
    $property = $Json.PSObject.Properties[$Name]
    if ($null -ne $property) {
      $value = $property.Value
    }
  }
  if ($null -eq $value) {
    return @()
  }
  if ($value -is [System.Array]) {
    return @($value)
  }
  if ($value -is [System.Collections.IEnumerable] -and $value -isnot [string]) {
    return @($value)
  }
  return @($value)
}

function Invoke-MultipartImageUpload {
  param(
    [string]$Uri,
    [string]$FilePath
  )
  try {
    $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec $httpTimeoutSec -Uri $Uri -Method POST -Form @{
      file = Get-Item $FilePath
    } -Headers @{ Accept = "application/json" }
    $json = $null
    if (-not [string]::IsNullOrWhiteSpace([string]$response.Content)) {
      $json = $response.Content | ConvertFrom-Json
    }
    return [pscustomobject]@{
      StatusCode = [int]$response.StatusCode
      Json = $json
      Content = [string]$response.Content
    }
  } catch {
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
      return [pscustomobject]@{
        StatusCode = [int]$_.Exception.Response.StatusCode
        Json = $null
        Content = ""
      }
    }
    if ($_.Exception.StatusCode) {
      return [pscustomobject]@{
        StatusCode = [int]$_.Exception.StatusCode
        Json = $null
        Content = ""
      }
    }
    throw
  }
}

try {
  $psi = [System.Diagnostics.ProcessStartInfo]::new()
  $psi.FileName = $Executable
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.Environment["USERPROFILE"] = $smokeHome
  $psi.Environment["LOCALAPPDATA"] = $localAppData
  $psi.Environment["APPDATA"] = $roamingAppData
  $psi.Environment["LUMEN_DATA_ROOT"] = $dataRoot
  $psi.Environment["LUMEN_DESKTOP_HEADLESS_SMOKE"] = "1"
  $psi.Environment.Remove("HTTP_PROXY") | Out-Null
  $psi.Environment.Remove("HTTPS_PROXY") | Out-Null
  $psi.Environment.Remove("ALL_PROXY") | Out-Null
  $psi.Environment.Remove("http_proxy") | Out-Null
  $psi.Environment.Remove("https_proxy") | Out-Null
  $psi.Environment.Remove("all_proxy") | Out-Null

  $appProcess = [System.Diagnostics.Process]::Start($psi)
  $stdoutTask = $appProcess.StandardOutput.ReadToEndAsync()
  $stderrTask = $appProcess.StandardError.ReadToEndAsync()

  $logsRoot = Join-Path $dataRoot "data/logs"
  $apiPort = $null
  $webPort = $null
  $baselineReady = $false
  $deadline = (Get-Date).AddSeconds(75)
  while ((Get-Date) -lt $deadline) {
    if (-not (Test-Path $logsRoot)) {
      $matches = Get-ChildItem -Path $work -Directory -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "[/\\]data[/\\]logs$" }
      if ($matches) {
        $logsRoot = $matches[0].FullName
      }
    }
    if ($logsRoot -and (Test-Path $logsRoot)) {
      $apiErr = Join-Path $logsRoot "api.err.log"
      $webLog = Join-Path $logsRoot "web.log"
      if (Test-Path $apiErr) {
        $text = Get-Content $apiErr -Raw -ErrorAction SilentlyContinue
        if ($text -match "Uvicorn running on http://127\.0\.0\.1:(\d+)") {
          $apiPort = [int]$Matches[1]
        }
      }
      if (Test-Path $webLog) {
        $text = Get-Content $webLog -Raw -ErrorAction SilentlyContinue
        if ($text -match "Local:\s+http://(?:localhost|127\.0\.0\.1):(\d+)") {
          $webPort = [int]$Matches[1]
        }
      }
      if ($apiPort -and $webPort) {
        try {
          $api = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$apiPort/system/desktop-ready"
          $web = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$webPort/"
          if ($api.StatusCode -eq 200 -and $web.StatusCode -eq 200) {
            $baselineReady = $true
            break
          }
        } catch {
	    Start-Sleep -Milliseconds 250
  }
      }
    }
    if ($appProcess.HasExited) {
      break
    }
    Start-Sleep -Milliseconds 250
  }

  $operationErrors = [System.Collections.Generic.List[string]]::new()
  if ($baselineReady -and $webPort) {
    try {
      $created = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/conversations" -Body @{
        title = "desktop smoke"
      }
      $convId = if ($created.Json) { [string]$created.Json.id } else { "" }
      if ($created.StatusCode -ne 200 -or [string]::IsNullOrWhiteSpace($convId)) {
        $operationErrors.Add("desktop conversation create did not return an id")
      } else {
        $escapedId = [System.Uri]::EscapeDataString($convId)
        $patched = Invoke-JsonRequest -Method "PATCH" -Uri "http://127.0.0.1:$webPort/api/conversations/$escapedId" -Body @{
          title = "desktop smoke updated"
        }
        if ($patched.StatusCode -ne 200 -or -not $patched.Json -or $patched.Json.title -ne "desktop smoke updated") {
          $operationErrors.Add("desktop conversation patch did not persist title")
        }
        $loaded = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/conversations/$escapedId"
        if ($loaded.StatusCode -ne 200) {
          $operationErrors.Add("desktop conversation get returned $($loaded.StatusCode)")
        }
        $deleted = Invoke-JsonRequest -Method "DELETE" -Uri "http://127.0.0.1:$webPort/api/conversations/$escapedId"
        if ($deleted.StatusCode -ne 200 -or -not $deleted.Json -or $deleted.Json.ok -ne $true) {
          $operationErrors.Add("desktop conversation delete did not return ok=true")
        }
      }
    } catch {
      $operationErrors.Add("desktop conversation CRUD request failed: $($_.Exception.Message)")
    }
    try {
      $prompts = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/system-prompts"
      if ($prompts.StatusCode -ne 200 -or -not $prompts.Json) {
        $operationErrors.Add("desktop system prompts list did not return 200")
      }
      $prompt = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/system-prompts" -Body @{
        name = "Desktop Smoke Prompt"
        content = "You are a desktop smoke test."
        make_default = $true
      }
      $promptId = if ($prompt.Json) { [string]$prompt.Json.id } else { "" }
      if ($prompt.StatusCode -ne 200 -or [string]::IsNullOrWhiteSpace($promptId) -or $prompt.Json.is_default -ne $true) {
        $operationErrors.Add("desktop system prompt create did not return a default prompt")
      } else {
        $escapedPromptId = [System.Uri]::EscapeDataString($promptId)
        $patchedPrompt = Invoke-JsonRequest -Method "PATCH" -Uri "http://127.0.0.1:$webPort/api/system-prompts/$escapedPromptId" -Body @{
          name = "Desktop Smoke Prompt Updated"
          content = "Updated desktop smoke prompt."
          make_default = $false
        }
        if ($patchedPrompt.StatusCode -ne 200 -or -not $patchedPrompt.Json -or $patchedPrompt.Json.name -ne "Desktop Smoke Prompt Updated") {
          $operationErrors.Add("desktop system prompt patch did not persist name")
        }
        $defaultedPrompt = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/system-prompts/$escapedPromptId/default"
        if ($defaultedPrompt.StatusCode -ne 200 -or -not $defaultedPrompt.Json -or $defaultedPrompt.Json.is_default -ne $true) {
          $operationErrors.Add("desktop system prompt default did not persist")
        }
        $deletedPrompt = Invoke-JsonRequest -Method "DELETE" -Uri "http://127.0.0.1:$webPort/api/system-prompts/$escapedPromptId"
        if ($deletedPrompt.StatusCode -ne 204) {
          $operationErrors.Add("desktop system prompt delete returned $($deletedPrompt.StatusCode)")
        }
      }
    } catch {
      $operationErrors.Add("desktop system prompt CRUD request failed: $($_.Exception.Message)")
    }
    try {
      $providerName = "desktop-smoke-provider"
      $providers = Invoke-JsonRequest -Method "PUT" -Uri "http://127.0.0.1:$webPort/api/settings/providers" -Body @{
        items = @(
          @{
            name = $providerName
            base_url = "http://127.0.0.1:9"
            api_key = "sk-desktop-smoke-key"
            priority = 0
            weight = 1
            enabled = $true
            purposes = @("chat", "image")
            image_jobs_enabled = $true
            image_jobs_endpoint = "generations"
            image_jobs_endpoint_lock = $true
            image_jobs_base_url = ""
            image_edit_input_transport = "url"
            image_concurrency = 1
          }
        )
        proxies = @()
      }
      $providerItems = if ($providers.Json) { @($providers.Json.items) } else { @() }
      if (
        $providers.StatusCode -ne 200 -or
        $providerItems.Count -lt 1 -or
        $providerItems[0].name -ne $providerName -or
        $providerItems[0].enabled -ne $true -or
        $providerItems[0].api_key_hint -eq "sk-desktop-smoke-key"
      ) {
        $operationErrors.Add("desktop providers PUT did not persist masked provider")
      }
      $probe = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/settings/providers/probe" -Body @{
        names = @($providerName)
      }
      $probeItems = if ($probe.Json) { @($probe.Json.items) } else { @() }
      if (
        $probe.StatusCode -ne 200 -or
        $probeItems.Count -lt 1 -or
        $probeItems[0].name -ne $providerName -or
        $probeItems[0].status -ne "skipped" -or
        $probeItems[0].error -ne "endpoint_locked_to_generations"
      ) {
        $operationErrors.Add("desktop providers probe did not skip generation-locked provider")
      }
      $escapedProviderName = [System.Uri]::EscapeDataString($providerName)
      $disabledProvider = Invoke-JsonRequest -Method "PATCH" -Uri "http://127.0.0.1:$webPort/api/settings/providers/$escapedProviderName/enabled" -Body @{
        enabled = $false
      }
      if ($disabledProvider.StatusCode -ne 200 -or -not $disabledProvider.Json -or $disabledProvider.Json.enabled -ne $false) {
        $operationErrors.Add("desktop provider enabled PATCH did not persist false")
      }
      $stats = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/settings/providers/stats"
      $statItems = if ($stats.Json) { @($stats.Json.items) } else { @() }
      if (
        $stats.StatusCode -ne 200 -or
        -not ($statItems | Where-Object { $_.name -eq $providerName })
      ) {
        $operationErrors.Add("desktop provider stats did not include saved provider")
      }
      $cleared = Invoke-JsonRequest -Method "PUT" -Uri "http://127.0.0.1:$webPort/api/settings/providers" -Body @{
        items = @()
        proxies = @()
      }
      $clearedItems = if ($cleared.Json) { @($cleared.Json.items) } else { @("__missing__") }
      if ($cleared.StatusCode -ne 200 -or $clearedItems.Count -ne 0) {
        $operationErrors.Add("desktop providers clear did not return empty items")
      }
    } catch {
      $operationErrors.Add("desktop providers save/probe/clear request failed: $($_.Exception.Message)")
    }
    try {
      $memorySettings = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/me/memory-settings"
      if ($memorySettings.StatusCode -ne 200 -or -not $memorySettings.Json) {
        $operationErrors.Add("desktop memory settings did not return 200")
      }
      $memorySettings = Invoke-JsonRequest -Method "PATCH" -Uri "http://127.0.0.1:$webPort/api/me/memory-settings" -Body @{
        paused = $true
        confirmation_enabled = $true
      }
      if ($memorySettings.StatusCode -ne 200 -or -not $memorySettings.Json -or $memorySettings.Json.paused -ne $true -or $memorySettings.Json.confirmation_enabled -ne $true) {
        $operationErrors.Add("desktop memory settings patch did not persist")
      }
      $onboarding = Invoke-JsonRequest -Method "PATCH" -Uri "http://127.0.0.1:$webPort/api/me/onboarding-seen" -Body @{
        flag = 2
      }
      if ($onboarding.StatusCode -ne 200 -or -not $onboarding.Json -or (([int]$onboarding.Json.onboarding_seen -band (1 -shl 2)) -eq 0)) {
        $operationErrors.Add("desktop memory onboarding flag did not persist")
      }
      $scopes = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/me/memory-scopes"
      if ($scopes.StatusCode -ne 200 -or -not $scopes.Json) {
        $operationErrors.Add("desktop memory scopes list did not return 200")
      }
      $scope = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/me/memory-scopes" -Body @{
        name = "Desktop Smoke Scope"
        emoji = "DS"
      }
      $scopeId = if ($scope.Json) { [string]$scope.Json.id } else { "" }
      if ($scope.StatusCode -ne 200 -or [string]::IsNullOrWhiteSpace($scopeId)) {
        $operationErrors.Add("desktop memory scope create did not return an id")
      } else {
        $escapedScopeId = [System.Uri]::EscapeDataString($scopeId)
        $patchedScope = Invoke-JsonRequest -Method "PATCH" -Uri "http://127.0.0.1:$webPort/api/me/memory-scopes/$escapedScopeId" -Body @{
          name = "Desktop Smoke Scope Renamed"
          emoji = "DR"
        }
        if ($patchedScope.StatusCode -ne 200 -or -not $patchedScope.Json -or $patchedScope.Json.name -ne "Desktop Smoke Scope Renamed" -or $patchedScope.Json.emoji -ne "DR") {
          $operationErrors.Add("desktop memory scope patch did not persist")
        }
        $memoryConv = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/conversations" -Body @{
          title = "desktop memory smoke"
        }
        $memoryConvId = if ($memoryConv.Json) { [string]$memoryConv.Json.id } else { "" }
        if ($memoryConv.StatusCode -ne 200 -or [string]::IsNullOrWhiteSpace($memoryConvId)) {
          $operationErrors.Add("desktop memory conversation create failed")
        } else {
          $escapedMemoryConvId = [System.Uri]::EscapeDataString($memoryConvId)
          $activeScope = Invoke-JsonRequest -Method "PATCH" -Uri "http://127.0.0.1:$webPort/api/conversations/$escapedMemoryConvId/active-scope" -Body @{
            scope_id = $scopeId
          }
          if ($activeScope.StatusCode -ne 200 -or -not $activeScope.Json -or $activeScope.Json.scope_id -ne $scopeId) {
            $operationErrors.Add("desktop conversation active memory scope did not persist")
          }
          $memoryDisabled = Invoke-JsonRequest -Method "PATCH" -Uri "http://127.0.0.1:$webPort/api/conversations/$escapedMemoryConvId/memory-disabled" -Body @{
            disabled = $true
          }
          if ($memoryDisabled.StatusCode -ne 200 -or -not $memoryDisabled.Json -or $memoryDisabled.Json.disabled -ne $true) {
            $operationErrors.Add("desktop conversation memory disable did not persist")
          }
          $usedMemories = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/conversations/$escapedMemoryConvId/used-memories"
          if ($usedMemories.StatusCode -ne 200 -or -not $usedMemories.Json) {
            $operationErrors.Add("desktop conversation used memories did not return 200")
          }
        }
        $memory = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/me/memories" -Body @{
          type = "preference"
          content = "Desktop smoke memory preference"
          pinned = $true
          scope_id = $scopeId
        }
        $memoryId = if ($memory.Json) { [string]$memory.Json.id } else { "" }
        if ($memory.StatusCode -ne 200 -or [string]::IsNullOrWhiteSpace($memoryId) -or $memory.Json.pinned -ne $true) {
          $operationErrors.Add("desktop memory create did not return a pinned memory")
        } else {
          $escapedMemoryId = [System.Uri]::EscapeDataString($memoryId)
          $patchedMemory = Invoke-JsonRequest -Method "PATCH" -Uri "http://127.0.0.1:$webPort/api/me/memories/$escapedMemoryId" -Body @{
            content = "Desktop smoke memory updated"
            pinned = $false
          }
          if ($patchedMemory.StatusCode -ne 200 -or -not $patchedMemory.Json -or $patchedMemory.Json.content -ne "Desktop smoke memory updated" -or $patchedMemory.Json.pinned -ne $false) {
            $operationErrors.Add("desktop memory patch did not persist")
          }
          $scopedMemory = Invoke-JsonRequest -Method "PATCH" -Uri "http://127.0.0.1:$webPort/api/me/memories/$escapedMemoryId/scope" -Body @{
            scope_id = $scopeId
          }
          if ($scopedMemory.StatusCode -ne 200 -or -not $scopedMemory.Json -or $scopedMemory.Json.scope_id -ne $scopeId) {
            $operationErrors.Add("desktop memory scope assignment did not persist")
          }
          $confirmedMemory = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/me/memories/$escapedMemoryId/confirm" -Body @{
            decision = "yes"
          }
          if ($confirmedMemory.StatusCode -ne 200 -or -not $confirmedMemory.Json -or $null -eq $confirmedMemory.Json.last_confirmed_at) {
            $operationErrors.Add("desktop memory confirm did not persist")
          }
          $memories = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/me/memories?type=preference&pinned=false&disabled=false&scope_id=$escapedScopeId"
          $memoryItems = if ($memories.Json) { @($memories.Json.items) } else { @() }
          if (
            $memories.StatusCode -ne 200 -or
            $memoryItems.Count -lt 1 -or
            -not ($memoryItems | Where-Object { $_.id -eq $memoryId })
          ) {
            $operationErrors.Add("desktop memories filtered list did not include saved memory")
          }
          $staging = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/me/memories/staging"
          if ($staging.StatusCode -ne 200 -or -not $staging.Json -or $null -eq $staging.Json.items) {
            $operationErrors.Add("desktop memory staging list did not return 200")
          }
          $timeline = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/me/memories/timeline?limit=5"
          $timelineItems = if ($timeline.Json) { @($timeline.Json.items) } else { @() }
          if ($timeline.StatusCode -ne 200 -or $timelineItems.Count -lt 1) {
            $operationErrors.Add("desktop memory timeline did not include audit rows")
          }
          $exported = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/me/memories/export"
          if ($exported.StatusCode -ne 200 -or -not $exported.Json) {
            $operationErrors.Add("desktop memories export did not return 200")
          }
          $clearMemory = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/me/memories" -Body @{
            type = "avoid"
            content = "Desktop smoke memory to clear"
            scope_id = $scopeId
          }
          $clearMemoryId = if ($clearMemory.Json) { [string]$clearMemory.Json.id } else { "" }
          if ($clearMemory.StatusCode -ne 200 -or [string]::IsNullOrWhiteSpace($clearMemoryId)) {
            $operationErrors.Add("desktop memory clear fixture create did not return an id")
          }
          $deletedMemory = Invoke-JsonRequest -Method "DELETE" -Uri "http://127.0.0.1:$webPort/api/me/memories/$escapedMemoryId"
          if ($deletedMemory.StatusCode -ne 200 -or -not $deletedMemory.Json -or $deletedMemory.Json.ok -ne $true) {
            $operationErrors.Add("desktop memory delete did not return ok=true")
          }
          $clearedMemories = Invoke-JsonRequest -Method "DELETE" -Uri "http://127.0.0.1:$webPort/api/me/memories" -Headers @{
            "X-Confirm-Clear-Memory" = "yes"
          }
          if ($clearedMemories.StatusCode -ne 200 -or -not $clearedMemories.Json -or [int]$clearedMemories.Json.deleted -lt 1) {
            $operationErrors.Add("desktop memory clear did not delete rows")
          }
        }
        if (-not [string]::IsNullOrWhiteSpace($memoryConvId)) {
          $escapedMemoryConvId = [System.Uri]::EscapeDataString($memoryConvId)
          $null = Invoke-JsonRequest -Method "DELETE" -Uri "http://127.0.0.1:$webPort/api/conversations/$escapedMemoryConvId"
        }
        $deletedScope = Invoke-JsonRequest -Method "DELETE" -Uri "http://127.0.0.1:$webPort/api/me/memory-scopes/$escapedScopeId"
        if ($deletedScope.StatusCode -ne 200 -or -not $deletedScope.Json -or $null -eq $deletedScope.Json.moved) {
          $operationErrors.Add("desktop memory scope delete did not return moved count")
        }
      }
    } catch {
      $operationErrors.Add("desktop memory CRUD request failed: $($_.Exception.Message)")
    }
    try {
      $feed = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/generations/feed?limit=1"
      $feedHasItems = ($null -ne $feed.HashJson) -and $feed.HashJson.ContainsKey("items")
      $feedHasTotal = ($null -ne $feed.HashJson) -and $feed.HashJson.ContainsKey("total")
      if ($feed.StatusCode -ne 200 -or -not $feedHasItems -or -not $feedHasTotal) {
        $operationErrors.Add("desktop generations feed did not return an item list")
      }
      $invalidFeed = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/generations/feed?ratio=bad-ratio"
      if ($invalidFeed.StatusCode -ne 400) {
        $operationErrors.Add("desktop generations feed invalid ratio returned $($invalidFeed.StatusCode)")
      }
      $pngPath = Join-Path $work "desktop-smoke.png"
      [System.IO.File]::WriteAllBytes(
        $pngPath,
        [System.Convert]::FromBase64String("iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8z8DAwMDAxMDAwMDAAAANHQEDasKb6QAAAABJRU5ErkJggg==")
      )
      $uploaded = Invoke-MultipartImageUpload -Uri "http://127.0.0.1:$webPort/api/images/upload" -FilePath $pngPath
      $imageId = if ($uploaded.Json) { [string]$uploaded.Json.id } else { "" }
      if (
        $uploaded.StatusCode -ne 200 -or
        [string]::IsNullOrWhiteSpace($imageId) -or
        [int]$uploaded.Json.width -ne 2 -or
        [int]$uploaded.Json.height -ne 2 -or
        [string]$uploaded.Json.mime -ne "image/png" -or
        [string]$uploaded.Json.url -ne "/api/images/$imageId/binary"
      ) {
        $operationErrors.Add("desktop image upload did not return expected metadata")
      } else {
        $escapedImageId = [System.Uri]::EscapeDataString($imageId)
        $meta = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/images/$escapedImageId"
        $normalizedRef = if ($meta.Json -and $meta.Json.metadata_jsonb) { $meta.Json.metadata_jsonb.normalized_ref } else { $null }
        if ($meta.StatusCode -ne 200 -or -not $meta.Json -or [string]$meta.Json.id -ne $imageId -or $null -eq $normalizedRef) {
          $operationErrors.Add("desktop image metadata did not include normalized_ref")
        }
        $binary = Get-HttpStatus -Uri "http://127.0.0.1:$webPort/api/images/$escapedImageId/binary"
        if ($binary -ne 200) {
          $operationErrors.Add("desktop image binary did not return 200")
        }
        $variant = Get-HttpStatus -Uri "http://127.0.0.1:$webPort/api/images/$escapedImageId/variants/display2048"
        if ($variant -ne 200) {
          $operationErrors.Add("desktop image display variant did not return 200")
        }
        $share = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/images/$escapedImageId/share" -Body @{
          show_prompt = $false
        }
        $shareId = if ($share.Json) { [string]$share.Json.id } else { "" }
        $shareToken = if ($share.Json) { [string]$share.Json.token } else { "" }
        if ($share.StatusCode -ne 201 -or [string]::IsNullOrWhiteSpace($shareId) -or [string]::IsNullOrWhiteSpace($shareToken) -or [string]$share.Json.image_id -ne $imageId) {
          $operationErrors.Add("desktop image share create did not return a token")
        } else {
          $escapedShareId = [System.Uri]::EscapeDataString($shareId)
          $escapedShareToken = [System.Uri]::EscapeDataString($shareToken)
          $shareList = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/me/shares"
          $shareItems = if ($shareList.Json) { @($shareList.Json.items) } else { @() }
          if (
            $shareList.StatusCode -ne 200 -or
            $shareItems.Count -lt 1 -or
            -not ($shareItems | Where-Object { $_.id -eq $shareId })
          ) {
            $operationErrors.Add("desktop share list did not include created share")
          }
          $publicShare = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/share/$escapedShareToken"
          $publicImages = if ($publicShare.Json) { @($publicShare.Json.images) } else { @() }
          $firstPublicImage = if ($publicImages.Count -gt 0) { $publicImages[0] } else { $null }
          if ($publicShare.StatusCode -ne 200 -or -not $publicShare.Json -or $publicShare.Json.token -ne $shareToken -or $null -eq $firstPublicImage -or [string]$firstPublicImage.id -ne $imageId) {
            $operationErrors.Add("desktop public share metadata did not include uploaded image")
          } else {
            $displayUrl = [string]$firstPublicImage.display_url
            if ([string]::IsNullOrWhiteSpace($displayUrl) -or -not $displayUrl.StartsWith("/api/share/")) {
              $operationErrors.Add("desktop public share metadata did not include display variant")
            } else {
              $displayStatus = Get-HttpStatus -Uri "http://127.0.0.1:$webPort$displayUrl"
              if ($displayStatus -ne 200) {
                $operationErrors.Add("desktop public share display variant did not return 200")
              }
            }
          }
          $publicImage = Get-HttpStatus -Uri "http://127.0.0.1:$webPort/api/share/$escapedShareToken/image"
          if ($publicImage -ne 200) {
            $operationErrors.Add("desktop public share image did not return 200")
          }
          $publicImageById = Get-HttpStatus -Uri "http://127.0.0.1:$webPort/api/share/$escapedShareToken/images/$escapedImageId"
          if ($publicImageById -ne 200) {
            $operationErrors.Add("desktop public share image-by-id did not return 200")
          }
          $invalidVariant = Get-HttpStatus -Uri "http://127.0.0.1:$webPort/api/share/$escapedShareToken/images/$escapedImageId/variants/bad-kind"
          if ($invalidVariant -ne 400) {
            $operationErrors.Add("desktop public share invalid variant did not return 400")
          }
          $revokedShare = Invoke-JsonRequest -Method "DELETE" -Uri "http://127.0.0.1:$webPort/api/shares/$escapedShareId"
          if ($revokedShare.StatusCode -ne 204) {
            $operationErrors.Add("desktop share revoke returned $($revokedShare.StatusCode)")
          }
          $revokedPublicShare = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/share/$escapedShareToken"
          if ($revokedPublicShare.StatusCode -ne 404) {
            $operationErrors.Add("desktop revoked share did not return 404")
          }
        }
        $multiShare = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/images/share" -Body @{
          image_ids = @($imageId)
          show_prompt = $false
        }
        $multiShareId = if ($multiShare.Json) { [string]$multiShare.Json.id } else { "" }
        $multiShareToken = if ($multiShare.Json) { [string]$multiShare.Json.token } else { "" }
        $multiImageIds = @(Get-JsonArrayProperty -Json $multiShare.Json -HashJson $multiShare.HashJson -Name "image_ids")
        if ($multiShare.StatusCode -ne 201 -or [string]::IsNullOrWhiteSpace($multiShareId) -or [string]::IsNullOrWhiteSpace($multiShareToken) -or $multiImageIds.Count -ne 1 -or [string]$multiImageIds[0] -ne $imageId) {
          $operationErrors.Add("desktop multi-image share create did not return image_ids: status=$($multiShare.StatusCode) content=$($multiShare.Content)")
        } else {
          $escapedMultiShareId = [System.Uri]::EscapeDataString($multiShareId)
          $escapedMultiShareToken = [System.Uri]::EscapeDataString($multiShareToken)
          $multiPublicImageById = Get-HttpStatus -Uri "http://127.0.0.1:$webPort/api/share/$escapedMultiShareToken/images/$escapedImageId"
          if ($multiPublicImageById -ne 200) {
            $operationErrors.Add("desktop multi-image public image-by-id did not return 200")
          }
          $revokedMultiShare = Invoke-JsonRequest -Method "DELETE" -Uri "http://127.0.0.1:$webPort/api/shares/$escapedMultiShareId"
          if ($revokedMultiShare.StatusCode -ne 204) {
            $operationErrors.Add("desktop multi-image share revoke returned $($revokedMultiShare.StatusCode)")
          }
        }
        $deletedImage = Invoke-JsonRequest -Method "DELETE" -Uri "http://127.0.0.1:$webPort/api/images/$escapedImageId"
        if ($deletedImage.StatusCode -ne 200 -or -not $deletedImage.Json -or $deletedImage.Json.ok -ne $true) {
          $operationErrors.Add("desktop image delete did not return ok=true")
        }
        $binaryAfterDelete = Get-HttpStatus -Uri "http://127.0.0.1:$webPort/api/images/$escapedImageId/binary"
        if ($binaryAfterDelete -ne 404) {
          $operationErrors.Add("desktop image binary after delete did not return 404")
        }
      }
    } catch {
      $operationErrors.Add("desktop image and feed requests failed: $($_.Exception.Message)")
    }
  } else {
    $operationErrors.Add("desktop conversation CRUD skipped before baseline readiness")
  }

  $workerRestarted = $false
  $treeIdsBefore = @(Get-ProcessTreeIds -ProcessId $appProcess.Id)
  $treeBefore = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $treeIdsBefore -contains ([int]$_.ProcessId) }
  $workerBefore = @($treeBefore | Where-Object {
    $_.Name -ieq "lumen-worker.exe" -or $_.CommandLine -like "*lumen-worker*"
  })
  if ($workerBefore.Count -gt 0) {
    $oldWorkerIds = @($workerBefore | ForEach-Object { [int]$_.ProcessId })
    Stop-Process -Id $oldWorkerIds[0] -Force -ErrorAction SilentlyContinue
    $restartDeadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $restartDeadline) {
      $treeIdsAfter = @(Get-ProcessTreeIds -ProcessId $appProcess.Id)
      $treeAfter = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $treeIdsAfter -contains ([int]$_.ProcessId) }
      $workerAfter = @($treeAfter | Where-Object {
        ($_.Name -ieq "lumen-worker.exe" -or $_.CommandLine -like "*lumen-worker*") -and
          ($oldWorkerIds -notcontains ([int]$_.ProcessId))
      })
      if ($workerAfter.Count -gt 0) {
        $workerRestarted = $true
        break
      }
      if ($appProcess.HasExited) {
        break
      }
      Start-Sleep -Milliseconds 250
    }
  }

  $webRestarted = $false
  $treeIdsBefore = @(Get-ProcessTreeIds -ProcessId $appProcess.Id)
  $treeBefore = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $treeIdsBefore -contains ([int]$_.ProcessId) }
  $webListenerIds = if ($webPort) { @(Get-ListeningProcessIds -Port $webPort) } else { @() }
  $webBefore = @($treeBefore | Where-Object {
    $_.Name -ieq "lumen-web.exe" -or
      $_.CommandLine -like "*lumen-web*" -or
      $_.CommandLine -like "*server.js*" -or
      ($webListenerIds -contains ([int]$_.ProcessId))
  })
  if ($webBefore.Count -gt 0) {
    $oldWebIds = @($webBefore | ForEach-Object { [int]$_.ProcessId })
    Stop-Process -Id $oldWebIds[0] -Force -ErrorAction SilentlyContinue
    $restartDeadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $restartDeadline) {
      $treeIdsAfter = @(Get-ProcessTreeIds -ProcessId $appProcess.Id)
      $treeAfter = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $treeIdsAfter -contains ([int]$_.ProcessId) }
      $webListenerIdsAfter = if ($webPort) { @(Get-ListeningProcessIds -Port $webPort) } else { @() }
      $webAfter = @($treeAfter | Where-Object {
        (
          $_.Name -ieq "lumen-web.exe" -or
          $_.CommandLine -like "*lumen-web*" -or
          $_.CommandLine -like "*server.js*" -or
          ($webListenerIdsAfter -contains ([int]$_.ProcessId))
        ) -and ($oldWebIds -notcontains ([int]$_.ProcessId))
      })
      $webOk = $false
      if ($webPort) {
        try {
          $web = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$webPort/"
          $webOk = $web.StatusCode -eq 200
        } catch {
          $webOk = $false
        }
      }
      if ($webAfter.Count -gt 0 -and $webOk) {
        $webRestarted = $true
        break
      }
      if ($appProcess.HasExited) {
        break
      }
      Start-Sleep -Milliseconds 250
    }
  }

  $apiRestarted = $false
  $treeIdsBefore = @(Get-ProcessTreeIds -ProcessId $appProcess.Id)
  $treeBefore = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $treeIdsBefore -contains ([int]$_.ProcessId) }
  $apiBefore = @($treeBefore | Where-Object {
    $_.Name -ieq "lumen-api.exe" -or $_.CommandLine -like "*lumen-api*"
  })
  if ($apiBefore.Count -gt 0) {
    $oldApiIds = @($apiBefore | ForEach-Object { [int]$_.ProcessId })
    Stop-Process -Id $oldApiIds[0] -Force -ErrorAction SilentlyContinue
    $restartDeadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $restartDeadline) {
      $treeIdsAfter = @(Get-ProcessTreeIds -ProcessId $appProcess.Id)
      $treeAfter = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $treeIdsAfter -contains ([int]$_.ProcessId) }
      $apiAfter = @($treeAfter | Where-Object {
        ($_.Name -ieq "lumen-api.exe" -or $_.CommandLine -like "*lumen-api*") -and
          ($oldApiIds -notcontains ([int]$_.ProcessId))
      })
      $sidecarNames = @("lumen-api", "lumen-worker", "lumen-redis", "lumen-web")
      $aliveCount = 0
      foreach ($name in $sidecarNames) {
        if ($name -eq "lumen-web") {
          $webListenerIdsAfter = if ($webPort) { @(Get-ListeningProcessIds -Port $webPort) } else { @() }
          $alive = [bool]($treeAfter | Where-Object {
            $_.Name -ieq "lumen-web.exe" -or
              $_.CommandLine -like "*lumen-web*" -or
              $_.CommandLine -like "*server.js*" -or
              ($webListenerIdsAfter -contains ([int]$_.ProcessId))
          })
        } else {
          $alive = [bool]($treeAfter | Where-Object {
            $_.Name -ieq "$name.exe" -or $_.CommandLine -like "*$name*"
          })
        }
        if ($alive) {
          $aliveCount += 1
        }
      }
      $readyAgain = $false
      if ($apiPort -and $webPort) {
        try {
          $api = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$apiPort/system/desktop-ready"
          $web = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$webPort/"
          $readyAgain = $api.StatusCode -eq 200 -and $web.StatusCode -eq 200
        } catch {
          $readyAgain = $false
        }
      }
      if ($apiAfter.Count -gt 0 -and $aliveCount -eq 4 -and $readyAgain) {
        $apiRestarted = $true
        break
      }
      if ($appProcess.HasExited) {
        break
      }
      Start-Sleep -Milliseconds 250
    }
  }

  if ($apiRestarted -and $apiPort -and $webPort) {
    Start-Sleep -Seconds 2
  }

  $logs = @{}
  foreach ($name in @("supervisor.log", "redis.log", "redis.err.log", "api.log", "api.err.log", "worker.err.log", "web.log", "web.err.log")) {
    $path = if ($logsRoot) { Join-Path $logsRoot $name } else { "" }
    $logs[$name] = if ($path -and (Test-Path $path)) { Get-Content $path -Raw -ErrorAction SilentlyContinue } else { "" }
  }
  $treeIds = @(Get-ProcessTreeIds -ProcessId $appProcess.Id)
  $treeProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $treeIds -contains ([int]$_.ProcessId) }
  $processes = @{}
  $webListenerIds = if ($webPort) { @(Get-ListeningProcessIds -Port $webPort) } else { @() }
  foreach ($name in @("lumen-api", "lumen-worker", "lumen-redis", "lumen-web")) {
    if ($name -eq "lumen-web") {
      $processes[$name] = [bool]($treeProcesses | Where-Object {
        $_.Name -ieq "lumen-web.exe" -or
          $_.CommandLine -like "*lumen-web*" -or
          $_.CommandLine -like "*server.js*" -or
          ($webListenerIds -contains ([int]$_.ProcessId))
      })
    } else {
      $processes[$name] = [bool]($treeProcesses | Where-Object {
        $_.Name -ieq "$name.exe" -or $_.CommandLine -like "*$name*"
      })
    }
  }

  Write-Host "logs_root=$logsRoot"
  Write-Host "api_port=$apiPort web_port=$webPort"
  Write-Host "baseline_ready=$($baselineReady.ToString().ToLowerInvariant())"
  Write-Host "worker_restarted=$($workerRestarted.ToString().ToLowerInvariant())"
  Write-Host "web_restarted=$($webRestarted.ToString().ToLowerInvariant())"
  Write-Host "api_restarted=$($apiRestarted.ToString().ToLowerInvariant())"
  Write-Host ("processes " + (($processes.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value.ToString().ToLowerInvariant())" }) -join " "))
  foreach ($name in $logs.Keys) {
    Write-Host "--- $name tail ---"
    $text = [string]$logs[$name]
    if ($text.Length -gt 1600) {
      Write-Host $text.Substring($text.Length - 1600)
    } else {
      Write-Host $text
    }
  }

  $combined = ($logs.Values -join "`n")
  $errors = [System.Collections.Generic.List[string]]::new()
  foreach ($item in $operationErrors) {
    $errors.Add($item)
  }
  if ($combined.Contains("--logdir") -or $combined.Contains("LogDir specified without enabling tiered storage")) {
    $errors.Add("old Garnet logdir failure is present")
  }
  if ([string]$logs["worker.err.log"] -match "api_key is required") {
    $errors.Add("worker rejects disabled desktop provider without api_key")
  }
  if ($combined.Contains("context_window.tiktoken_unavailable")) {
    $errors.Add("packaged Python runtime could not load tiktoken")
  }
  if ($combined.Contains("context_window.tiktoken_loading_slow")) {
    $errors.Add("packaged Python runtime fell back before tiktoken warmed")
  }
  if ($combined.Contains("Lua scripting support disabled")) {
    $errors.Add("redis lua scripting is disabled")
  }
  if ($combined.Contains("Unknown Redis command called from script") -or $combined.Contains("sse dedupe reservation has no stream id")) {
    $errors.Add("redis lua xadd fallback did not handle Garnet")
  }
  if ($combined.Contains("api publish_sse_event xadd failed") -or $combined.Contains("api publish_sse_events xadd batch failed") -or $combined.Contains("publish_event: XADD failed")) {
    $errors.Add("redis stream xadd fallback did not handle Garnet")
  }
  if ([string]$logs["web.log"] -match "Network:\s+http://(?!(?:localhost|127\.0\.0\.1)(?::|/))|0\.0\.0\.0") {
    $errors.Add("web runtime is listening on a non-loopback interface")
  }
  if (-not ([string]$logs["supervisor.log"]).Contains('"event":"heartbeat"')) {
    $errors.Add("supervisor heartbeat event was not logged")
  }
  if (-not ([string]$logs["supervisor.log"]).Contains('"event":"sidecar_restart"')) {
    $errors.Add("supervisor sidecar_restart event was not logged")
  }
  if (-not ([string]$logs["supervisor.log"]).Contains('"event":"full_restart"')) {
    $errors.Add("supervisor full_restart event was not logged")
  }
  if (-not $baselineReady) {
    $errors.Add("baseline desktop readiness was not reached")
  }
  if (-not ([string]$logs["redis.log"]).Contains("Ready to accept connections")) {
    $errors.Add("redis readiness not proven")
  }
  if ($workerBefore.Count -eq 0) {
    $errors.Add("worker process was not present before restart probe")
  } elseif (-not $workerRestarted) {
    $errors.Add("worker process did not restart after termination")
  }
  if ($webBefore.Count -eq 0) {
    $errors.Add("web process was not present before restart probe")
  } elseif (-not $webRestarted) {
    $errors.Add("web process did not restart after termination")
  }
  if ($apiBefore.Count -eq 0) {
    $errors.Add("api process was not present before critical restart probe")
  } elseif (-not $apiRestarted) {
    $errors.Add("api critical restart did not recover the full stack")
  }
  if (-not $apiPort -or -not $webPort) {
    $errors.Add("api/web ports not discovered")
  } else {
    try {
      $ready = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$apiPort/system/desktop-ready"
      if ($ready.StatusCode -ne 200) {
        $errors.Add("api desktop-ready did not return 200")
      }
    } catch {
      $errors.Add("api desktop-ready request failed: $($_.Exception.Message)")
    }
    try {
      $directAuth = Get-HttpStatus -Uri "http://127.0.0.1:$apiPort/auth/me"
      if ($directAuth -ne 401) {
        $errors.Add("direct api auth/me without desktop token did not return 401")
      }
    } catch {
      $errors.Add("direct api auth/me request failed: $($_.Exception.Message)")
    }
    try {
      $directActivity = Get-HttpStatus -Uri "http://127.0.0.1:$apiPort/system/desktop-activity"
      if ($directActivity -ne 401) {
        $errors.Add("direct api desktop-activity without desktop token did not return 401")
      }
    } catch {
      $errors.Add("direct api desktop-activity request failed: $($_.Exception.Message)")
    }
    try {
      $authMe = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$webPort/api/auth/me"
      if ($authMe.StatusCode -ne 200) {
        $errors.Add("web proxy auth/me did not return 200")
      }
    } catch {
      $errors.Add("web proxy auth/me request failed: $($_.Exception.Message)")
    }
    try {
      $conversations = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$webPort/api/conversations?limit=1"
      if ($conversations.StatusCode -ne 200) {
        $errors.Add("web proxy conversations did not return 200")
      }
    } catch {
      $errors.Add("web proxy conversations request failed: $($_.Exception.Message)")
    }
    try {
      $activity = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$webPort/api/system/desktop-activity"
      if ($activity.StatusCode -ne 200) {
        $errors.Add("web proxy desktop-activity did not return 200")
      }
    } catch {
      $errors.Add("web proxy desktop-activity request failed: $($_.Exception.Message)")
    }
    $desktopRoutes = @(
      "/",
      "/assets",
      "/stream",
      "/me",
      "/settings/providers",
      "/settings/storage",
      "/settings/diagnostics",
      "/settings/update",
      "/settings/memory",
      "/settings/prompts"
    )
    foreach ($route in $desktopRoutes) {
      try {
        $status = Get-HttpStatus -Uri "http://127.0.0.1:$webPort$route"
        if ($status -ne 200) {
          $errors.Add("desktop web route $route did not return 200")
        }
      } catch {
        $errors.Add("desktop web route $route request failed: $($_.Exception.Message)")
      }
    }
    $dockerOnlyRoutes = @(
      "/admin",
      "/login",
      "/projects",
      "/me/wallet",
      "/settings/api-key",
      "/settings/privacy",
      "/settings/telegram",
      "/settings/usage"
    )
    foreach ($route in $dockerOnlyRoutes) {
      try {
        $status = Get-HttpStatusNoRedirect -Uri "http://127.0.0.1:$webPort$route"
        if (@(301, 302, 303, 307, 308) -notcontains $status) {
          $errors.Add("desktop unsupported route $route did not redirect")
        }
      } catch {
        $errors.Add("desktop unsupported route $route request failed: $($_.Exception.Message)")
      }
    }
    $apiGets = [ordered]@{
      "/api/auth/me" = 200
      "/api/auth/csrf" = 200
      "/api/settings/bootstrap-status" = 200
      "/api/settings/diagnostics" = 200
      "/api/settings/system" = 200
      "/api/settings/providers" = 200
      "/api/settings/providers/stats" = 200
      "/api/conversations?limit=1" = 200
      "/api/generations/feed?limit=1" = 200
      "/api/system/desktop-activity" = 200
    }
    foreach ($path in $apiGets.Keys) {
      try {
        $status = Get-HttpStatus -Uri "http://127.0.0.1:$webPort$path"
        if ($status -ne $apiGets[$path]) {
          $errors.Add("desktop web proxy $path returned $status, expected $($apiGets[$path])")
        }
      } catch {
        $errors.Add("desktop web proxy $path request failed: $($_.Exception.Message)")
      }
    }
    try {
      $csrf = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/auth/csrf"
      if ($csrf.StatusCode -ne 200 -or -not $csrf.Json -or $csrf.Json.csrf_token -ne "desktop-local-token") {
        $errors.Add("desktop csrf did not return desktop-local-token")
      }
    } catch {
      $errors.Add("desktop csrf request failed: $($_.Exception.Message)")
    }
    try {
      $logout = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/auth/logout"
      if ($logout.StatusCode -ne 204) {
        $errors.Add("desktop logout returned $($logout.StatusCode)")
      }
      $authAfterLogout = Get-HttpStatus -Uri "http://127.0.0.1:$webPort/api/auth/me"
      if ($authAfterLogout -ne 200) {
        $errors.Add("desktop auth/me failed after logout no-op")
      }
    } catch {
      $errors.Add("desktop logout request failed: $($_.Exception.Message)")
    }
    try {
      $bootstrap = Invoke-JsonRequest -Method "POST" -Uri "http://127.0.0.1:$webPort/api/settings/bootstrap-complete" -Body @{
        settings = @{
          theme = "system"
          language = "zh-CN"
          auto_check_updates = $true
          crash_reports_enabled = $false
        }
      }
      if ($bootstrap.StatusCode -ne 200 -or -not $bootstrap.Json -or $bootstrap.Json.complete -ne $true) {
        $errors.Add("desktop bootstrap-complete did not return complete=true")
      }
    } catch {
      $errors.Add("desktop bootstrap-complete request failed: $($_.Exception.Message)")
    }
    try {
      $bootstrapStatus = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/settings/bootstrap-status"
      if ($bootstrapStatus.StatusCode -ne 200 -or -not $bootstrapStatus.Json -or $bootstrapStatus.Json.complete -ne $true) {
        $errors.Add("desktop bootstrap status did not persist complete=true")
      }
    } catch {
      $errors.Add("desktop bootstrap-status request failed: $($_.Exception.Message)")
    }
    try {
      $diagnostics = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/settings/diagnostics"
      $expectedDataRoot = ([System.IO.Path]::GetFullPath($dataRoot)).Replace("\", "/").TrimEnd("/")
      $actualDataRoot = if ($diagnostics.Json) { ([string]$diagnostics.Json.data_root).Replace("\", "/").TrimEnd("/") } else { "" }
      $logsRootValue = if ($diagnostics.Json) { ([string]$diagnostics.Json.logs_root).Replace("\", "/") } else { "" }
      $settingsPath = if ($diagnostics.Json) { ([string]$diagnostics.Json.settings_path).Replace("\", "/") } else { "" }
      $providerMetadataPath = if ($diagnostics.Json) { ([string]$diagnostics.Json.provider_metadata_path).Replace("\", "/") } else { "" }
      $diskFreeBytes = if ($diagnostics.Json) { [int64]$diagnostics.Json.disk_free_bytes } else { 0 }
      if (
        $diagnostics.StatusCode -ne 200 -or
        -not $diagnostics.Json -or
        $actualDataRoot -ne $expectedDataRoot -or
        -not $logsRootValue.EndsWith("/data/logs") -or
        -not $settingsPath.EndsWith("/data/settings.json") -or
        -not $providerMetadataPath.EndsWith("/data/providers.json") -or
        $diagnostics.Json.bootstrap_complete -ne $true -or
        $diskFreeBytes -le 0
      ) {
        $errors.Add("desktop diagnostics payload did not match runtime state")
      }
    } catch {
      $errors.Add("desktop diagnostics request failed: $($_.Exception.Message)")
    }
    try {
      $settings = Invoke-JsonRequest -Method "PUT" -Uri "http://127.0.0.1:$webPort/api/settings/system" -Body @{
        items = @(
          @{ key = "providers.auto_probe_interval"; value = "0" },
          @{ key = "providers.auto_image_probe_interval"; value = "0" }
        )
      }
      if ($settings.StatusCode -ne 200) {
        $errors.Add("desktop settings/system PUT returned $($settings.StatusCode)")
      }
    } catch {
      $errors.Add("desktop settings/system PUT request failed: $($_.Exception.Message)")
    }
    try {
      $unsupportedSettings = Invoke-JsonRequest -Method "PUT" -Uri "http://127.0.0.1:$webPort/api/settings/system" -Body @{
        items = @(
          @{ key = "billing.enabled"; value = "true" }
        )
      }
      if ($unsupportedSettings.StatusCode -ne 422) {
        $errors.Add("desktop settings/system unsupported key returned $($unsupportedSettings.StatusCode)")
      }
    } catch {
      $errors.Add("desktop settings/system unsupported key request failed: $($_.Exception.Message)")
    }
    try {
      $invalidSettings = Invoke-JsonRequest -Method "PUT" -Uri "http://127.0.0.1:$webPort/api/settings/system" -Body @{
        items = @(
          @{ key = "providers.auto_probe_interval"; value = "not-an-int" }
        )
      }
      if ($invalidSettings.StatusCode -ne 422) {
        $errors.Add("desktop settings/system invalid value returned $($invalidSettings.StatusCode)")
      }
    } catch {
      $errors.Add("desktop settings/system invalid value request failed: $($_.Exception.Message)")
    }
  }
  $deadProcessCount = @($processes.Values | Where-Object { -not $_ }).Count
  if ($deadProcessCount -ne 0) {
    $errors.Add("not all sidecar processes are alive")
  }

  if ($errors.Count -gt 0) {
    if ($appProcess.HasExited) {
      Write-Host "app_exit_code=$($appProcess.ExitCode)"
    } else {
      Write-Host "app_exit_code=running"
    }
    if ($stdoutTask.IsCompleted) {
      Set-Content -Path $stdoutPath -Value $stdoutTask.Result -Encoding UTF8
      Write-Host "--- app.stdout.log tail ---"
      $stdoutText = [string]$stdoutTask.Result
      if ($stdoutText.Length -gt 1600) {
        Write-Host $stdoutText.Substring($stdoutText.Length - 1600)
      } else {
        Write-Host $stdoutText
      }
    }
    if ($stderrTask.IsCompleted) {
      Set-Content -Path $stderrPath -Value $stderrTask.Result -Encoding UTF8
      Write-Host "--- app.stderr.log tail ---"
      $stderrText = [string]$stderrTask.Result
      if ($stderrText.Length -gt 1600) {
        Write-Host $stderrText.Substring($stderrText.Length - 1600)
      } else {
        Write-Host $stderrText
      }
    }
    foreach ($errorItem in $errors) {
      Write-Host "ERROR: $errorItem"
    }
    exit 1
  }

  Write-Host "win_launch_smoke_ok"
} finally {
  if ($appProcess -and -not $appProcess.HasExited) {
    Stop-ProcessTree -ProcessId $appProcess.Id
  }
  Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
