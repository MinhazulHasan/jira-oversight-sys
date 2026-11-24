<#
.SYNOPSIS
    The morning run: a fresh cycle with the rules, then the daily digest.

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
