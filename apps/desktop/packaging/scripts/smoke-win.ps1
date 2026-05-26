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
        if ($text -match "Local:\s+http://localhost:(\d+)") {
          $webPort = [int]$Matches[1]
        }
      }
      if ($apiPort -and $webPort) {
        try {
          $api = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$apiPort/system/desktop-ready"
          $web = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "http://127.0.0.1:$webPort/"
          if ($api.StatusCode -eq 200 -and $web.StatusCode -eq 200) {
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
  if ($combined.Contains("--logdir") -or $combined.Contains("LogDir specified without enabling tiered storage")) {
    $errors.Add("old Garnet logdir failure is present")
  }
  if ([string]$logs["worker.err.log"] -match "api_key is required") {
    $errors.Add("worker rejects disabled desktop provider without api_key")
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
