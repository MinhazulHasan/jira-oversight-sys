<#
.SYNOPSIS
    Builds the dashboard as a self-contained Next.js server (output: standalone) and puts it in the folder IIS runs it from.

.DESCRIPTION
    Runs from the dashboard's source folder (jira-oversight-ui): `npm ci` with no install script, then `npm run build`
    with STANDALONE_OUTPUT=1, then copies what the server needs into -DestinationPath: server.js and its traced
    node_modules, .next\static and public. Nothing else is copied: no source, no .env file, no test output. Every
    setting is read when the server starts, from the dashboard's env file (dashboard.env.example), so the build holds
    none and the same folder serves any deployment.

    -DestinationPath is mirrored: what the folder holds that the build does not is deleted, so keep nothing of your own
    in it (the env file, the logs and web.config live elsewhere). Stop the dashboard's site first when it is running
    (docs/DEPLOYMENT_IIS.md, "Updating"): the server holds its files open.

    Supports -WhatIf: it prints what it would do and changes nothing.

.PARAMETER SourcePath
    The dashboard's source folder. Default: jira-oversight-ui beside the backend's folder.

.PARAMETER DestinationPath
    Where the server goes, for example D:\JiraOversight\dashboard.

.PARAMETER SkipInstall
    Skip `npm ci` (the dependencies are already in place from the same package-lock.json).

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy\iis\Publish-Dashboard.ps1 -DestinationPath D:\JiraOversight\dashboard -WhatIf
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $SourcePath = '',
    [Parameter(Mandatory)]
    [string] $DestinationPath,
    [switch] $SkipInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$backend = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $SourcePath) { $SourcePath = Join-Path (Split-Path -Parent $backend) 'jira-oversight-ui' }
if (-not (Test-Path -LiteralPath (Join-Path $SourcePath 'package.json') -PathType Leaf)) {
    throw "No dashboard source at $SourcePath (no package.json): pass -SourcePath."
}
$SourcePath = (Resolve-Path -LiteralPath $SourcePath).Path
if (-not [System.IO.Path]::IsPathRooted($DestinationPath)) { $DestinationPath = Join-Path (Get-Location).Path $DestinationPath }
$DestinationPath = [System.IO.Path]::GetFullPath($DestinationPath)
if ($DestinationPath -eq $SourcePath -or $DestinationPath.StartsWith($SourcePath + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "The destination must be outside the source folder ($SourcePath)."
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Node.js is not installed (docs/DEPLOYMENT_IIS.md, section 3).' }

function Invoke-Native([string] $Title, [scriptblock] $Command) {
    Write-Host $Title
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Title failed (exit $LASTEXITCODE)." }
}

Push-Location -LiteralPath $SourcePath
try {
    if (-not $SkipInstall) {
        if ($PSCmdlet.ShouldProcess($SourcePath, 'npm ci --ignore-scripts')) {
            Invoke-Native 'Installing the dependencies (npm ci)' { npm.cmd ci --ignore-scripts --no-audit --no-fund }
        }
    }
    if ($PSCmdlet.ShouldProcess($SourcePath, 'npm run build (standalone output)')) {
        $env:STANDALONE_OUTPUT = '1'
        $env:NEXT_TELEMETRY_DISABLED = '1'
        try {
            Invoke-Native 'Building the dashboard (npm run build)' { npm.cmd run build }
        } finally {
            Remove-Item Env:\STANDALONE_OUTPUT, Env:\NEXT_TELEMETRY_DISABLED -ErrorAction SilentlyContinue
        }
    }

    $standalone = Join-Path $SourcePath '.next\standalone'
    if ($PSCmdlet.ShouldProcess($DestinationPath, 'mirror the standalone server, .next\static and public')) {
        if (-not (Test-Path -LiteralPath (Join-Path $standalone 'server.js') -PathType Leaf)) {
            throw "The build left no $standalone\server.js: was it built with STANDALONE_OUTPUT=1?"
        }
        # robocopy exit codes below 8 mean success. No .env file ever travels, whatever the source folder holds.
        $copies = @(
            @($standalone, $DestinationPath, @('/MIR', '/XF', '.env', '.env.*')),
            @((Join-Path $SourcePath '.next\static'), (Join-Path $DestinationPath '.next\static'), @('/MIR')),
            @((Join-Path $SourcePath 'public'), (Join-Path $DestinationPath 'public'), @('/MIR'))
        )
        foreach ($copy in $copies) {
            $from, $to, $options = $copy
            if (-not (Test-Path -LiteralPath $from)) { continue }
            robocopy $from $to @options /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "Copying $from to $to failed (robocopy exit $LASTEXITCODE)." }
        }
        $global:LASTEXITCODE = 0
        Write-Host "Done: $DestinationPath\server.js"
    }
} finally {
    Pop-Location
}
