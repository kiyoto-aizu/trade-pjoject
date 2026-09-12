param(
    [string]$StopFile = ""
)

if ([string]::IsNullOrWhiteSpace($StopFile)) {
    $StopFile = Join-Path $PSScriptRoot "..\data\emergency_stop"
}

$parent = Split-Path -Parent $StopFile
New-Item -ItemType Directory -Force -Path $parent | Out-Null
Set-Content -Path $StopFile -Value (Get-Date -Format o) -Encoding utf8
Write-Output "Emergency stop requested: $StopFile"