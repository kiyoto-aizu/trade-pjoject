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

. (Join-Path $scriptsRoot 'common\stderr_logging.ps1')
$stderrLog = Initialize-StderrLogging -ProjectRoot $projectRoot -ScriptName 'run_backtest'

# stderrへのINFOログ出力を終端エラー扱いさせないため、ネイティブ実行時のみContinueにする
$ErrorActionPreference = 'Continue'
Push-Location $projectRoot
try {
    & $python -m src.entrypoints.run_backtest `
        --filtering-dir data/filtering `
        --live `
        --days 730 `
        --minute-bars-dir data/minute_bars_parquet `
        --indicator-source daily `
        --compare-market-regime `
        --output data/backtest/latest/latest_timeseries.json `
        --archive-directory data/backtest/runs 2>> $stderrLog
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    $today = (Get-Date).Date
    $daysFromMonday = ([int]$today.DayOfWeek + 6) % 7
    $weekStart = $today.AddDays(-$daysFromMonday)
    $weekEnd = $weekStart.AddDays(4)
    & $python -m src.entrypoints.run_backtest `
        --filtering-dir data/filtering `
        --live `
        --days 730 `
        --start-date $weekStart.ToString('yyyy-MM-dd') `
        --end-date $weekEnd.ToString('yyyy-MM-dd') `
        --minute-bars-dir data/minute_bars_parquet `
        --indicator-source daily `
        --compare-market-regime `
        --output data/backtest/latest/latest_weekly.json `
        --archive-directory data/backtest/runs 2>> $stderrLog
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}