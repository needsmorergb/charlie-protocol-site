<#
.SYNOPSIS
    Register (or remove) a Windows Scheduled Task that keeps the keeper running.

.DESCRIPTION
    Creates a task that runs Start-CharlieKeeper.ps1 from this folder at
    logon of the current user and again every hour if it is not already
    running -- so a reboot or a crash never leaves the keeper down for long.
    The keeper's budget lives in its state file, so being restarted by the
    task cannot make it spend more than keeper.json allows.

    Run from an elevated PowerShell if your policy requires it for
    Register-ScheduledTask. Requires Windows PowerShell 5.1 or PowerShell 7.

.EXAMPLE
    .\Register-CharlieKeeperTask.ps1            # register
    .\Register-CharlieKeeperTask.ps1 -Remove    # unregister
#>
[CmdletBinding()]
param(
    [string]$TaskName = "Charlie Protocol Keeper",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed task '$TaskName'"
    exit 0
}

$script = Join-Path $PSScriptRoot "Start-CharlieKeeper.ps1"
if (-not (Test-Path $script)) { throw "Start-CharlieKeeper.ps1 not found next to this script" }

$pwsh = (Get-Command pwsh -ErrorAction SilentlyContinue) ?? (Get-Command powershell)
$action = New-ScheduledTaskAction -Execute $pwsh.Source `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`"" `
    -WorkingDirectory $PSScriptRoot

$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Hours 1))
)

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Days 365) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "Runs the Charlie Protocol BURN-leg keeper from $PSScriptRoot. Stop it by creating keeper.stop there." | Out-Null

Write-Host "registered task '$TaskName' running $script"
Write-Host "It starts at logon and re-checks hourly. The keeper itself only spends what keeper.json allows."
