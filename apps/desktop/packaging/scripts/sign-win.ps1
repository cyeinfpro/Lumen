$ErrorActionPreference = "Stop"

param(
  [Parameter(Mandatory = $true)]
  [string]$Path
)

$TimestampUrl = if ($env:WINDOWS_TIMESTAMP_URL) { $env:WINDOWS_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }

Get-ChildItem -Recurse $Path -Include *.exe,*.dll | ForEach-Object {
  signtool sign /tr $TimestampUrl /td sha256 /fd sha256 /a $_.FullName
}

signtool verify /pa /v $Path
