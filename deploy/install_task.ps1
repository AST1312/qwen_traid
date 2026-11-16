<#
.SYNOPSIS
    Register morning portfolio check in Windows Task Scheduler.
.DESCRIPTION
    Creates task "QwenTraid Morning Portfolio Check":
      - Schedule: Mon-Fri at 10:15 (Europe/Moscow)
      - Action: deploy\run_morning_check.bat
      - Mode: "Run only when user is logged on" (SOCKS5 proxy at 127.0.0.1:10808
        via ProxyBridge is only accessible in the interactive user session)
      - Wake computer: YES
      - Run on battery: YES
.NOTES
    Run from PowerShell (not necessarily as admin - task is per-user).
#>

$ErrorActionPreference = "Stop"

$TaskName        = "QwenTraid Morning Portfolio Check"
$TaskDescription = "Daily morning portfolio check (qwen_traid): MOEX/Yahoo quotes, ACRA/Expert RA ratings, Telegram delivery."

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PywExe     = "C:\Windows\pyw.exe"
$PywScript  = Join-Path $PSScriptRoot "run_morning_check.pyw"

if (-not (Test-Path $PywExe)) {
    Write-Host "ERROR: $PywExe not found. Install Python launcher or adjust path." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $PywScript)) {
    Write-Host "ERROR: $PywScript not found" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "=== Installing Windows Scheduled Task (SILENT, no console window) ===" -ForegroundColor Cyan
Write-Host "Task name:  $TaskName"
Write-Host "Schedule:   Mon-Fri at 10:15"
Write-Host "Action:     $PywExe $PywScript"
Write-Host "User:       $env:USERNAME"
Write-Host ""

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$daysOfWeek = @([System.DayOfWeek]::Monday,
                 [System.DayOfWeek]::Tuesday,
                 [System.DayOfWeek]::Wednesday,
                 [System.DayOfWeek]::Thursday,
                 [System.DayOfWeek]::Friday)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $daysOfWeek -At 10:15
$action = New-ScheduledTaskAction -Execute $PywExe -Argument "`"$PywScript`"" -WorkingDirectory $ProjectDir

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description $TaskDescription `
        -Trigger $trigger `
        -Action $action `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null

    Write-Host "Task registered successfully!" -ForegroundColor Green
    Write-Host ""
    Write-Host "Verify in Task Scheduler (taskschd.msc):" -ForegroundColor Cyan
    Write-Host "  Task Scheduler Library -> '$TaskName'"
    Write-Host "  Right-click -> Run to test now"
    Write-Host ""
    Write-Host "Quick commands:" -ForegroundColor Cyan
    Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName'"
    Write-Host "  Get-Content '$ProjectDir\logs\watch-scheduler.log' -Tail 50"
    Write-Host "  Unregister-ScheduledTask -TaskName '$TaskName'"
    Write-Host ""
    Write-Host "IMPORTANT: Make sure ProxyBridge (SOCKS5 127.0.0.1:10808) starts" -ForegroundColor Yellow
    Write-Host "automatically with Windows (add to shell:startup), otherwise it may" -ForegroundColor Yellow
    Write-Host "not be running at 10:15 and Telegram delivery will fail." -ForegroundColor Yellow

} catch {
    Write-Host "ERROR: $_" -ForegroundColor Red
    Write-Host "Try running PowerShell as Administrator." -ForegroundColor Yellow
    exit 1
}
