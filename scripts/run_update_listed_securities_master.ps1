[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Error "Python virtual environment was not found: $python"
    exit 1
}

Push-Location $projectRoot
try {
    & $python scripts\update_listed_securities_master.py
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
