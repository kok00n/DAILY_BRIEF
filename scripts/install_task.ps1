# Registers a Windows Scheduled Task that builds the brief every morning.
# Runs whether you are logged on or not (S4U), and wakes the PC if asleep.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -At 06:00
#
# Remove with:  Unregister-ScheduledTask -TaskName DailyBrief -Confirm:$false
param(
    [string]$At = "06:00",
    [string]$TaskName = "DailyBrief"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    Write-Warning ".venv not found - run scripts\setup.ps1 first. Falling back to system pythonw."
    $pythonw = "pythonw.exe"
}
$script = Join-Path $root "run_brief.py"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 3)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' to run daily at $At." -ForegroundColor Green
Write-Host "Run it now to test:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Logs:  output\run_YYYYMMDD.log"
