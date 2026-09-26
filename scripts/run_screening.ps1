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

. (Join-Path $PSScriptRoot 'common\stderr_logging.ps1')
$stderrLog = Initialize-StderrLogging -ProjectRoot $projectRoot -ScriptName 'run_screening'

# stderrへのINFOログ出力を終端エラー扱いさせないため、ネイティブ実行時のみContinueにする
$ErrorActionPreference = 'Continue'
Push-Location $projectRoot
try {
    $arguments = @('-m', 'src.entrypoints.run_screening')
    if ($Date) {
        $arguments += @('--date', $Date)
    }
    & $python @arguments 2>> $stderrLog
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
