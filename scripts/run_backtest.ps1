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
    & $python -m src.entrypoints.run_backtest `
        --filtering-dir data/filtering `
        --live `
        --days 730 `
        --minute-bars-dir data/minute_bars `
        --indicator-source daily `
        --output data/backtest/latest_timeseries.json
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}