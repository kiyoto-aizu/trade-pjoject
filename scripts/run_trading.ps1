[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Error "Python virtual environment was not found: $python"
    exit 1
}

# 本番APIから価格を取得しても、注文は必ず仮想約定にする。
$env:TRADING_MODE = 'paper'
$env:ENABLE_LIVE_ORDERING = 'false'

Push-Location $projectRoot
try {
    & $python -m src.entrypoints.run_trading
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}