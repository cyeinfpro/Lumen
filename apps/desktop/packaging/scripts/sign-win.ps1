param(
  [Parameter(Mandatory = $true)]
  [string]$Path,
  [string]$Thumbprint = "",
  [string]$CertPath = "",
  [string]$CertPassword = ""
)

$ErrorActionPreference = "Stop"

$TimestampUrl = if ($env:WINDOWS_TIMESTAMP_URL) { $env:WINDOWS_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }

if (-not $Thumbprint -and $env:WINDOWS_SIGNING_THUMBPRINT) {
  $Thumbprint = $env:WINDOWS_SIGNING_THUMBPRINT
}
if (-not $CertPath -and $env:WINDOWS_SIGNING_CERT_PATH) {
  $CertPath = $env:WINDOWS_SIGNING_CERT_PATH
}
if (-not $CertPassword -and $env:WINDOWS_SIGNING_CERT_PASSWORD) {
  $CertPassword = $env:WINDOWS_SIGNING_CERT_PASSWORD
}

if (Test-Path -LiteralPath $Path -PathType Leaf) {
  $targets = @(
    Get-Item -LiteralPath $Path |
      Where-Object { $_.Extension -in @(".exe", ".dll") }
  )
} else {
  $targets = @(
    Get-ChildItem -Recurse -File -LiteralPath $Path |
      Where-Object { $_.Extension -in @(".exe", ".dll") }
  )
}

if ($targets.Count -eq 0) {
  throw "No Windows signable files (*.exe, *.dll) found at $Path"
}

$targets | ForEach-Object {
  $args = @("sign", "/tr", $TimestampUrl, "/td", "sha256", "/fd", "sha256")
  if ($Thumbprint) {
    $args += @("/sha1", $Thumbprint)
  } elseif ($CertPath) {
    $args += @("/f", $CertPath)
    if ($CertPassword) {
      $args += @("/p", $CertPassword)
    }
  } else {
    throw "Thumbprint or CertPath is required; refusing automatic certificate selection"
  }
  $args += $_.FullName
  & signtool @args
  if ($LASTEXITCODE -ne 0) {
    throw "signtool sign failed for $($_.FullName)"
  }

  signtool verify /pa /v $_.FullName
  if ($LASTEXITCODE -ne 0) {
    throw "signtool verify failed for $($_.FullName)"
  }
}
