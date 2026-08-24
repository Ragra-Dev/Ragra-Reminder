<#
.SYNOPSIS
  Registers (or re-registers) a Windows Task Scheduler task that runs
  `ragra tick` every 15 minutes, unattended, forever - the whole automation
  layer. No daemon, no extra infrastructure: Task Scheduler IS the scheduler.

.DESCRIPTION
  - MultipleInstances = IgnoreNew: if a tick is still running (e.g. a slow
    Classroom sync) when the next one would fire, the new one is skipped
    rather than overlapping - avoids concurrent syncs.
  - StartWhenAvailable: if the machine was asleep/off when a run was due,
    it catches up as soon as it's back, instead of silently losing that run.
  - Triggers on logon AND on a repeating 15-minute schedule that starts
    immediately, so it survives both reboots and sign-outs.
  - `ragra tick` itself (see ragra/cli.py) isolates Classroom sync,
    Calendar sync, and reminder dispatch from each other and logs to
    RAGRA_HOME\logs\ragra.log - this script only owns the "run it
    periodically" concern.

.USAGE
  Run from an elevated or normal PowerShell prompt (no admin required for a
  per-user task):
    powershell -ExecutionPolicy Bypass -File .\scripts\install-scheduled-task.ps1

  To remove it later:
    Unregister-ScheduledTask -TaskName "Ragra Tick" -Confirm:$false
#>

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$TaskName = "Ragra Tick"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Expected venv Python at '$PythonExe' - create it first (python -m venv .venv; .venv\Scripts\pip install -e .)."
    exit 1
}

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument "-m ragra.cli tick" -WorkingDirectory $ProjectDir

# [TimeSpan]::MaxValue is NOT valid here - PowerShell serializes it to a
# Task Scheduler XML duration ("P99999999DT23H59M59S") that exceeds what
# the schema accepts, so Register-ScheduledTask rejects the whole task. The
# Windows-supported way to repeat "every 15 minutes, forever" is a Daily
# trigger (which itself recurs every day with no end boundary) combined
# with a repetition window of one day (a valid, small ISO-8601 duration,
# "P1D") - the daily recurrence is what makes it indefinite, not the
# repetition duration.
#
# New-ScheduledTaskTrigger won't accept -Daily and -RepetitionInterval in
# the same call (they're in different parameter sets), so build the
# Repetition pattern via a throwaway -Once trigger and attach it to the
# real Daily trigger - the standard workaround for this module limitation.
$triggerRecurring = New-ScheduledTaskTrigger -Daily -At (Get-Date)
$triggerRecurring.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
# Scoped to this specific user, not left as "any user logs on": the
# unscoped form requires elevated privilege to register (confirmed by
# direct testing - it fails with the same Access Denied this whole fix is
# for) and isn't what's needed anyway on a single-user machine.
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Note: an S4U ("Service for User") principal was tried here to let the
# task run independent of desktop-session state, but registration fails
# with Access Denied on this machine - S4U requires the "Log on as a batch
# job" (SeBatchLogonRight) local security policy right for the account,
# which isn't granted here. That's a system security-policy setting, not
# something to change from a script. Falling back to the default principal
# (interactive-token based) - confirmed to register successfully - which
# means the task runs while the user is logged on, which the existing
# AtLogOn trigger already accounts for.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -DontStopOnIdleEnd

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($triggerRecurring, $triggerLogon) `
    -Settings $settings `
    -Description "Runs 'ragra tick' (Classroom sync, Calendar sync, reminder dispatch) every 15 minutes." `
    -Force | Out-Null

Write-Host "Installed scheduled task '$TaskName' - runs every 15 minutes, and at logon."
Write-Host "Logs: $env:LOCALAPPDATA\ragra\logs\ragra.log"
Write-Host "Check status:  Get-ScheduledTaskInfo -TaskName `"$TaskName`""
Write-Host "Run it now:    Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "Remove it:     Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"
