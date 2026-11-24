<#
.SYNOPSIS
    One scheduled job of the IIS deployment: the sync cycle, the nightly full cycle or the daily backup.

.PARAMETER Task
    Cycle, FullCycle or Backup.

.PARAMETER LogDir
    Folder for the log files. Default: logs\ in the backend's folder.

.PARAMETER Python
    The interpreter to run. Default: jira_env\Scripts\python.exe in the backend's folder.

.PARAMETER WaitMinutes
    How long a cycle waits for another that is still running. Default: 10.

.PARAMETER KeepDays
    Log files older than this many days are deleted. Default: 30; 0 keeps every file.

.PARAMETER PruneDir
    More folders whose *.log files older than -KeepDays are deleted, with Backup (the web server's stdout logs).

.PARAMETER LockName
    The name of the lock that keeps cycles apart.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy\iis\Invoke-OversightTask.ps1 -Task Cycle
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Cycle', 'FullCycle', 'Backup')]
    [string] $Task,
    [string] $LogDir = '',
    [string] $Python = '',
    [int] $WaitMinutes = 10,
    [int] $KeepDays = 30,
    [string] $PruneDir = '',
    [string] $LockName = 'Global\JiraOversightCycle'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# deploy\iis\ sits two folders below the backend's.
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $LogDir) { $LogDir = Join-Path $root 'logs' }
if (-not $Python) { $Python = Join-Path $root 'jira_env\Scripts\python.exe' }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogDir = (Resolve-Path -LiteralPath $LogDir).Path
$name = $Task.ToLowerInvariant()
$log = Join-Path $LogDir ('task_{0}_{1:yyyy-MM-dd}.log' -f $name, (Get-Date))
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Log([string] $Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss zzz} [{1}] {2}{3}' -f (Get-Date), $name, $Message, [Environment]::NewLine
    [System.IO.File]::AppendAllText($log, $line, $utf8)
}

function Invoke-Oversight([string[]] $Arguments) {
    $text = 'python -m oversight ' + ($Arguments -join ' ')
    Write-Log "start: $text"
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $env:OVERSIGHT_COMMAND = '"{0}" -m oversight {1} >> "{2}" 2>&1' -f $Python, ($Arguments -join ' '), $log
    & $env:ComSpec /d /c '%OVERSIGHT_COMMAND%' | Out-Null
    $code = $LASTEXITCODE
    Remove-Item Env:\OVERSIGHT_COMMAND
    Write-Log ('end: {0} (exit {1}, {2:N1} s)' -f $text, $code, $clock.Elapsed.TotalSeconds)
    return $code
}

function Remove-OldLogs([string] $Folder, [string] $Filter) {
    $cutoff = (Get-Date).AddDays(-$KeepDays)
    # A file the web server still has open stays: it is deleted by a later run.
    Get-ChildItem -LiteralPath $Folder -Filter $Filter -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        Remove-Item -ErrorAction SilentlyContinue
}

# Python writes UTF-8 to the log whatever the console's code page.
$env:PYTHONIOENCODING = 'utf-8'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Write-Log "FAILED: no Python at $Python (create jira_env first: docs/DEPLOYMENT_IIS.md, section 4)"
    exit 1
}

$exitCode = 0
Push-Location -LiteralPath $root
try {
    if ($Task -eq 'Backup') {
        Write-Log 'backup started'
        $exitCode = Invoke-Oversight @('backup')
        Write-Log $(if ($exitCode -eq 0) { 'backup finished: ok' } else { "backup FAILED (exit $exitCode)" })
        if ($KeepDays -gt 0) {
            Remove-OldLogs $LogDir 'task_*.log'
            foreach ($folder in ($PruneDir -split ';')) {
                if ($folder.Trim()) { Remove-OldLogs $folder.Trim() '*.log' }
            }
        }
    } else {
        # The lock scripts\run_cycle.ps1 takes too: one cycle at a time on this machine.
        $mutex = New-Object System.Threading.Mutex($false, $LockName)
        $acquired = $false
        try {
            try {
                $acquired = $mutex.WaitOne([TimeSpan]::FromMinutes($WaitMinutes))
            } catch [System.Threading.AbandonedMutexException] {
                $acquired = $true  # the previous cycle died holding the lock; it is ours now
            }
            if (-not $acquired) {
                Write-Log "skipped: another cycle is still running after $WaitMinutes minutes"
            } else {
                $arguments = if ($Task -eq 'FullCycle') { @('cycle', '--full') } else { @('cycle') }
                Write-Log "cycle started ($($arguments -join ' '))"
                $exitCode = Invoke-Oversight $arguments
                Write-Log $(if ($exitCode -eq 0) { 'cycle finished: ok' } else { "cycle FAILED (exit $exitCode)" })
            }
        } finally {
            if ($acquired) { $mutex.ReleaseMutex() }
            $mutex.Dispose()
        }
    }
} catch {
    # Anything unexpected in this script still ends up in the log, as a failure.
    Write-Log "FAILED: $($_.Exception.Message)"
    $exitCode = 1
} finally {
    Pop-Location
}
exit $exitCode
