[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'trade-pjoject-minute-backfill',
    [datetime]$At = [datetime]'07:30',
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run_minute_backfill.ps1'

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($TaskName, 'Remove scheduled task')) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
    }
    exit 0
}

if (-not (Test-Path $runner)) {
    throw "Minute backfill runner was not found: $runner"
}

# 週2回（月曜・木曜の朝07:30）、過去7日分の分足データを取得・上書きしてバックフィルする
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Thursday -At $At
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -WakeToRun

if ($PSCmdlet.ShouldProcess($TaskName, "Register bi-weekly (Monday, Thursday) minute-bar backfill task at $($At.ToString('HH:mm'))")) {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description 'Backfills self-polled minute bars with Yahoo Finance intraday data twice a week (Monday and Thursday) for the past 7 days.' `
        -Force | Out-Null
}
