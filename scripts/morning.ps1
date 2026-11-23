<#
.SYNOPSIS
    The morning run: a fresh cycle with the rules, then the daily digest.

.DESCRIPTION
    Meant for Windows Task Scheduler, once a day at a time that suits the reader
    (docs/REFERENCE.md, "Scheduling"). It runs run_cycle.ps1 -Rules -Digest: sync
    --incremental, derive, rules, baseline --missing, then digest, under the same lock and into the
    same log (logs\cycle_YYYY-MM-DD.log) as the 30-minute cycle, so the digest is
    built from the newest findings and never from a half-written cycle.

    The digest goes out even when an earlier step failed: it says at the top when
    the data may be stale, so a broken sync never looks like a quiet day. The
    exit code is the first failing step's (docs/REFERENCE.md, "Exit codes"); 0 means
    every step succeeded.

.PARAMETER Channel
    file, email or teams, instead of DIGEST_CHANNEL.

.PARAMETER LogDir
    Folder for the log files. Default: logs\ in the project root.

.PARAMETER Python
    The interpreter to run. Default: jira_env\Scripts\python.exe in the project root.

.PARAMETER WaitMinutes
    How long to wait for a cycle that is still running. Default: 10.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\morning.ps1

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\morning.ps1 -Channel email
#>
[CmdletBinding()]
param(
    [string] $Channel = '',
    [string] $LogDir = '',
    [string] $Python = '',
    [int] $WaitMinutes = 10
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$cycle = Join-Path $PSScriptRoot 'run_cycle.ps1'
& $cycle -Rules -Digest -Channel $Channel -LogDir $LogDir -Python $Python -WaitMinutes $WaitMinutes
exit $LASTEXITCODE
