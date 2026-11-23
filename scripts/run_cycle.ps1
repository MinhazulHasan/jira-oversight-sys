<#
.SYNOPSIS
    One oversight cycle: `discover`, `sync --incremental`, then `derive` (and `rules` with `baseline --missing`,
    `digest` when asked).

.DESCRIPTION
    Meant for Windows Task Scheduler, every 30 minutes (docs/REFERENCE.md, "Scheduling").
    Runs from the project root with jira_env's Python and appends everything the
    commands print (the reports on stdout, the JSON logs on stderr) to
    logs\cycle_YYYY-MM-DD.log. First it lists every board the Jira account can
    browse (`discover`, JO-17; nothing with BOARD_DISCOVERY=off): a failed search
    is noted in the log and the cycle goes on, as the boards stored before stay.
    Then it stops at the first command that fails and exits with that command's
    exit code (docs/REFERENCE.md, "Exit codes"); 0 means every step succeeded. sync,
    derive and rules cover JIRA_PROJECTS and every other board synced before.
    Only one cycle runs at a time on this machine: a cycle that cannot start
    within -WaitMinutes is skipped (exit 0, noted in the log).

.PARAMETER Full
    Read every issue (`sync --full`) instead of only what changed. Worth running
    once a night: only a full sync notices issues deleted or moved in Jira.

.PARAMETER Rules
    Run `rules` after `derive`, so findings open and resolve with every cycle,
    then `baseline --missing`: the baseline report of each synced board that has
    none yet (a board first synced from the dashboard, say). A baseline that
    fails is noted in the log and the cycle has still succeeded: the boards'
    figures are in, and the next cycle tries again.

.PARAMETER Digest
    Send the daily digest (`digest`) last. It goes out even when an earlier step
    failed: it says at the top when the data may be stale, so a broken sync never
    looks like a quiet day. scripts\morning.ps1 runs the cycle this way.

.PARAMETER Channel
    With -Digest: file, email or teams, instead of DIGEST_CHANNEL.

.PARAMETER LogDir
    Folder for the log files. Default: logs\ in the project root.

.PARAMETER Python
    The interpreter to run. Default: jira_env\Scripts\python.exe in the project root.

.PARAMETER WaitMinutes
    How long to wait for a cycle that is still running. Default: 10.

.PARAMETER KeepDays
    Log files older than this many days are deleted. Default: 30; 0 keeps every file.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\run_cycle.ps1

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\run_cycle.ps1 -Full

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\run_cycle.ps1 -Rules
#>
[CmdletBinding()]
param(
    [switch] $Full,
    [switch] $Rules,
    [switch] $Digest,
    [string] $Channel = '',
    [string] $LogDir = '',
    [string] $Python = '',
    [int] $WaitMinutes = 10,
    [int] $KeepDays = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
if (-not $LogDir) { $LogDir = Join-Path $root 'logs' }
if (-not $Python) { $Python = Join-Path $root 'jira_env\Scripts\python.exe' }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogDir = (Resolve-Path -LiteralPath $LogDir).Path
$log = Join-Path $LogDir ('cycle_{0:yyyy-MM-dd}.log' -f (Get-Date))
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Write-Log([string] $Message) {
    $line = '{0:yyyy-MM-dd HH:mm:ss zzz} [run_cycle] {1}{2}' -f (Get-Date), $Message, [Environment]::NewLine
    [System.IO.File]::AppendAllText($log, $line, $utf8)
}

function Invoke-Oversight([string[]] $Arguments) {
    $text = 'python -m oversight ' + ($Arguments -join ' ')
    Write-Log "start: $text"
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    # cmd.exe appends the bytes exactly as Python writes them (UTF-8, below), and
    # Python's stderr never turns into PowerShell errors (Windows PowerShell 5.1
    # would stop on the first JSON log line). The command travels in a variable:
    # cmd expands it before it parses quotes and redirections, so PowerShell never
    # has to quote it (its native argument quoting differs between versions).
    $env:OVERSIGHT_COMMAND = '"{0}" -m oversight {1} >> "{2}" 2>&1' -f $Python, ($Arguments -join ' '), $log
    & $env:ComSpec /d /c '%OVERSIGHT_COMMAND%' | Out-Null
    $code = $LASTEXITCODE
    Remove-Item Env:\OVERSIGHT_COMMAND
    Write-Log ('end: {0} (exit {1}, {2:N1} s)' -f $text, $code, $clock.Elapsed.TotalSeconds)
    return $code
}

# Python writes UTF-8 to the log whatever the console's code page.
$env:PYTHONIOENCODING = 'utf-8'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Write-Log "FAILED: no Python at $Python (create jira_env first; see README.md)"
    exit 1
}

if ($KeepDays -gt 0) {
    $cutoff = (Get-Date).AddDays(-$KeepDays)
    Get-ChildItem -LiteralPath $LogDir -Filter 'cycle_*.log' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -lt $cutoff } |
        Remove-Item -ErrorAction SilentlyContinue
}

try {
    $mutex = New-Object System.Threading.Mutex($false, 'Global\JiraOversightCycle')
} catch {
    Write-Log "FAILED: cannot take the cycle lock: $($_.Exception.Message)"
    exit 1
}
$acquired = $false
$exitCode = 0
Push-Location -LiteralPath $root
try {
    try {
        $acquired = $mutex.WaitOne([TimeSpan]::FromMinutes($WaitMinutes))
    } catch [System.Threading.AbandonedMutexException] {
        $acquired = $true  # the previous cycle died holding the lock; it is ours now
    }
    if (-not $acquired) {
        Write-Log "skipped: another cycle is still running after $WaitMinutes minutes"
    } else {
        $mode = if ($Full) { '--full' } else { '--incremental' }
        $steps = @('sync', 'derive')
        if ($Rules) { $steps += 'rules', 'baseline' }
        if ($Digest) { $steps += 'digest' }
        Write-Log ('cycle started (discover, then sync {0}, then {1})' -f $mode, (($steps | Select-Object -Skip 1) -join ', then '))
        # Board discovery (JO-17) never fails the cycle: the boards stored before stay.
        $code = Invoke-Oversight -Arguments @('discover')
        if ($code -ne 0) { Write-Log "discover failed (exit $code); the boards stored before stay" }
        $failed = ''
        foreach ($step in $steps) {
            # After a failure only the digest still runs: it flags stale data itself.
            if ($failed -and $step -ne 'digest') { continue }
            $arguments = switch ($step) {
                'sync' { @('sync', $mode) }
                'baseline' { @('baseline', '--missing') }
                'digest' { if ($Channel) { @('digest', '--channel', $Channel) } else { @('digest') } }
                default { @($step) }
            }
            $code = Invoke-Oversight $arguments
            if ($code -ne 0 -and $step -eq 'baseline') {
                # The boards' figures are in: a report still missing is written by the next cycle.
                Write-Log "baseline failed (exit $code); the next cycle writes the reports still missing"
                continue
            }
            if ($code -ne 0 -and -not $failed) {
                $failed = $step
                $exitCode = $code
            }
        }
        if (-not $failed) {
            Write-Log 'cycle finished: ok'
        } elseif ($Digest -and $failed -ne 'digest') {
            Write-Log "cycle FAILED at $failed (exit $exitCode); later steps were not run, except the digest"
        } else {
            Write-Log "cycle FAILED at $failed (exit $exitCode); later steps were not run"
        }
    }
} catch {
    # Anything unexpected in this script still ends up in the log, as a failure.
    Write-Log "cycle FAILED: $($_.Exception.Message)"
    $exitCode = 1
} finally {
    Pop-Location
    if ($acquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
exit $exitCode
