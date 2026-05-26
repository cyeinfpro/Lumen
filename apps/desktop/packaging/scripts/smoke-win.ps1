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
    [object]$Body = $null
  )
  $params = @{
    UseBasicParsing = $true
    TimeoutSec = $httpTimeoutSec
    Uri = $Uri
    Method = $Method
    Headers = @{ Accept = "application/json" }
  }
  if ($null -ne $Body) {
    $params["ContentType"] = "application/json"
    $params["Body"] = ($Body | ConvertTo-Json -Depth 8 -Compress)
  }
  try {
    $response = Invoke-WebRequest @params
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
          $memories = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/me/memories?disabled=false"
          if ($memories.StatusCode -ne 200 -or -not $memories.Json) {
            $operationErrors.Add("desktop memories list did not return 200")
          }
          $exported = Invoke-JsonRequest -Uri "http://127.0.0.1:$webPort/api/me/memories/export"
          if ($exported.StatusCode -ne 200 -or -not $exported.Json) {
            $operationErrors.Add("desktop memories export did not return 200")
          }
          $deletedMemory = Invoke-JsonRequest -Method "DELETE" -Uri "http://127.0.0.1:$webPort/api/me/memories/$escapedMemoryId"
          if ($deletedMemory.StatusCode -ne 200 -or -not $deletedMemory.Json -or $deletedMemory.Json.ok -ne $true) {
            $operationErrors.Add("desktop memory delete did not return ok=true")
          }
        }
        $deletedScope = Invoke-JsonRequest -Method "DELETE" -Uri "http://127.0.0.1:$webPort/api/me/memory-scopes/$escapedScopeId"
        if ($deletedScope.StatusCode -ne 200 -or -not $deletedScope.Json -or $null -eq $deletedScope.Json.moved) {
          $operationErrors.Add("desktop memory scope delete did not return moved count")
        }
      }
    } catch {
      $operationErrors.Add("desktop memory CRUD request failed: $($_.Exception.Message)")
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
