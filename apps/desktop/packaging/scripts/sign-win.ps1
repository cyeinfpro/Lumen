param(
  [Parameter(Mandatory = $true)]
  [string]$Path,
  [string]$Thumbprint = "",
  [string]$CertPath = "",
  [string]$CertPassword = ""
)

$ErrorActionPreference = "Stop"

$TimestampUrl = if ($env:WINDOWS_TIMESTAMP_URL) { $env:WINDOWS_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }

Get-ChildItem -Recurse $Path -Include *.exe,*.dll | ForEach-Object {
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
}

signtool verify /pa /v $Path
if ($LASTEXITCODE -ne 0) {
  throw "signtool verify failed for $Path"
}
