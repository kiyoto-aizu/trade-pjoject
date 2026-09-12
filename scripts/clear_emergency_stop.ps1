param(
    [string]$StopFile = ""
)

if ([string]::IsNullOrWhiteSpace($StopFile)) {
    $StopFile = Join-Path $PSScriptRoot "..\data\emergency_stop"
}

if (Test-Path $StopFile) {
    Remove-Item -Force $StopFile
    Write-Output "Emergency stop cleared: $StopFile"
} else {
    Write-Output "Emergency stop was not set: $StopFile"
}