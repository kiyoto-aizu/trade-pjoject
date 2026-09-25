[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Error "Python virtual environment was not found: $python"
    exit 1
}

. (Join-Path $PSScriptRoot 'common\stderr_logging.ps1')
$stderrLog = Initialize-StderrLogging -ProjectRoot $projectRoot -ScriptName 'run_update_listed_securities_master'

Push-Location $projectRoot
try {
    & $python scripts\update_listed_securities_master.py 2>> $stderrLog
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
