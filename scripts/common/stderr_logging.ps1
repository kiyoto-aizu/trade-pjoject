function Initialize-StderrLogging {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$ProjectRoot,
        [Parameter(Mandatory)]
        [string]$ScriptName,
        [int]$RetentionDays = 30
    )

    $logDir = Join-Path $ProjectRoot 'data\logs'
    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Path $logDir | Out-Null
    }

    $cutoff = (Get-Date).Date.AddDays(-$RetentionDays)
    Get-ChildItem -Path $logDir -Filter "${ScriptName}_stderr_*.log" -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        Remove-Item -Force

    $dateText = (Get-Date).ToString('yyyy-MM-dd')
    return (Join-Path $logDir "${ScriptName}_stderr_${dateText}.log")
}
