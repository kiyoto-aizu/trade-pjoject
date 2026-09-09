[CmdletBinding()]
param(
    [string]$Date
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Error "Python virtual environment was not found: $python"
    exit 1
}

Push-Location $projectRoot
try {
    $arguments = @('-m', 'src.entrypoints.run_filtering')
    if ($Date) {
        $arguments += @('--date', $Date)
    }
    & $python @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
