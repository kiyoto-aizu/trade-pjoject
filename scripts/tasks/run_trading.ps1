[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Python virtual environment was not found: $python"
    exit 1
}

$env:TRADING_MODE = 'paper'
$env:ENABLE_LIVE_ORDERING = 'false'

$logDir = Join-Path $projectRoot 'data\logs\jobs'
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
$dateText = (Get-Date).ToString('yyyy-MM-dd')
$stderrLog = Join-Path $logDir "run_trading_stderr_$dateText.log"

$ErrorActionPreference = 'Continue'
Push-Location $projectRoot
try {
    & $python -m src.entrypoints.run_trading 2>> $stderrLog
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
