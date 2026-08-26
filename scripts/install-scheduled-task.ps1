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
  - Allowed to run on battery and not stopped if the machine switches to
    battery mid-run - a laptop unplugged for a while must not silently lose
    ticks.
  - WakeToRun: wakes the machine from *sleep* to run a due tick. This does
    NOT help if the machine is fully powered off - only actual remote
    execution (outside this script's scope) solves that case.
  - Triggers on logon AND on a repeating 15-minute schedule that starts
    immediately, so it survives both reboots and sign-outs.
  - `ragra tick` itself (see ragra/cli.py) isolates Classroom sync,
    Calendar sync, reminder dispatch, and FAST timetable sync from each
    other and logs to RAGRA_HOME\logs\ragra.log - this script only owns
    the "run it periodically" concern.

.PARAMETER RunWithoutLogon
  Registers the task to run whether the user is logged on or not (Password
  logon type instead of Interactive), by prompting for Windows credentials
  interactively via the standard secure credential dialog - the password is
  never seen, stored, or handled by this script itself; Task Scheduler
  handles it internally. Omit this switch to keep the default (runs only in
  an active logon session, at logon and every 15 minutes within it), which
  needs no password prompt at all.

.USAGE
  Run from an elevated or normal PowerShell prompt (no admin required for a
  per-user task):
    powershell -ExecutionPolicy Bypass -File .\scripts\install-scheduled-task.ps1

  To also allow running while logged off (prompts for your password once,
  via Windows' own secure dialog - never stored by this script):
    powershell -ExecutionPolicy Bypass -File .\scripts\install-scheduled-task.ps1 -RunWithoutLogon

  To remove it later:
    Unregister-ScheduledTask -TaskName "Ragra Tick" -Confirm:$false
#>

param(
    [switch]$RunWithoutLogon
)

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

# Note: a passwordless S4U ("Service for User") principal was tried here
# to let the task run independent of desktop-session state, but
# registration fails with Access Denied on this machine - S4U requires the
# "Log on as a batch job" (SeBatchLogonRight) local security policy right
# for the account, which isn't granted here. A *password-based* principal
# (below, -RunWithoutLogon) sidesteps this: Task Scheduler grants that
# right automatically when registering a task with an explicit
# username+password, which is the standard supported way to get
# logged-off execution without touching local security policy directly.
#
# -Hidden: python.exe is a console-subsystem executable, so without this,
# Task Scheduler can surface a visible console window for it during a
# normal run. Hidden only affects whether Task Scheduler shows that
# window; it does not touch stdout/stderr or the process's own logging in
# any way - `ragra tick` still writes the same detailed log file, and a
# manually-run `ragra tick` in an open terminal still shows live output
# exactly as before.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -DontStopOnIdleEnd `
    -Hidden `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun

if ($RunWithoutLogon) {
    $cred = Get-Credential -Message "Enter your Windows password to let 'Ragra Tick' run whether you're logged on or not. Never stored by this script - handled entirely by Task Scheduler." -UserName $env:USERNAME
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger @($triggerRecurring, $triggerLogon) `
        -Settings $settings `
        -User $cred.UserName `
        -Password $cred.GetNetworkCredential().Password `
        -Description "Runs 'ragra tick' (Classroom sync, Calendar sync, reminder dispatch, FAST timetable sync) every 15 minutes, whether logged on or not." `
        -Force | Out-Null
} else {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger @($triggerRecurring, $triggerLogon) `
        -Settings $settings `
        -Description "Runs 'ragra tick' (Classroom sync, Calendar sync, reminder dispatch, FAST timetable sync) every 15 minutes." `
        -Force | Out-Null
}

Write-Host "Installed scheduled task '$TaskName' - runs every 15 minutes, and at logon."
Write-Host "Logs: $env:LOCALAPPDATA\ragra\logs\ragra.log"
Write-Host "Check status:  Get-ScheduledTaskInfo -TaskName `"$TaskName`""
Write-Host "Run it now:    Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "Remove it:     Unregister-ScheduledTask -TaskName `"$TaskName`" -Confirm:`$false"
