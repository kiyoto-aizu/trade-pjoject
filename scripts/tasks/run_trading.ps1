[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$scriptsRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = Split-Path -Parent $scriptsRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Error "Python virtual environment was not found: $python"
    exit 1
}

# 本番APIから価格を取得しても、注文は必ず仮想約定にする。
$env:TRADING_MODE = 'paper'
$env:ENABLE_LIVE_ORDERING = 'false'

# configure_logging()が呼ばれる前(importエラー等)の異常を日付別stderrログへ保存する。
. (Join-Path $scriptsRoot 'common\stderr_logging.ps1')
$stderrLog = Initialize-StderrLogging -ProjectRoot $projectRoot -ScriptName 'run_trading'

# stderrへのINFOログ出力を終端エラー扱いさせないため、ネイティブ実行時のみContinueにする
$ErrorActionPreference = 'Continue'
Push-Location $projectRoot
try {
    & $python -m src.entrypoints.run_trading 2>> $stderrLog
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}