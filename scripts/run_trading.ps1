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

# configure_logging()が呼ばれる前(importエラー等)の異常はアプリ側のログに
# 一切残らないため、stderrだけここで別ファイルに捕捉する。中身が空のままなら正常。
$logDir = Join-Path $projectRoot 'data\logs'
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}
$stderrLog = Join-Path $logDir 'run_trading_stderr.log'

Push-Location $projectRoot
try {
    & $python -m src.entrypoints.run_trading 2>> $stderrLog
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}