<#
.SYNOPSIS
    Sets up Jira Oversight on IIS: two app pools, two sites, folder permissions, the settings files and three scheduled jobs.

.PARAMETER HostName
    The address people open, without a scheme, for example oversight.example.com.

.PARAMETER CertificateThumbprint
    The thumbprint of the HTTPS certificate in Cert:\LocalMachine\My (it must name -HostName).

.PARAMETER InstallRoot
    The folder that holds everything but the backend. Default: the folder above the backend's.

.PARAMETER BackendPath
    The backend's folder (jira-oversight-sys). Default: the folder this script is in, two levels up.

.PARAMETER DashboardPath
    The built dashboard (Publish-Dashboard.ps1). Default: <InstallRoot>\dashboard.

.PARAMETER DashboardEnvFile
    The dashboard's settings file. Default: <InstallRoot>\dashboard.env.

.PARAMETER SitesPath
    The folder for the two sites' own folders. Default: <InstallRoot>\sites.

.PARAMETER Python
    The backend's interpreter. Default: <BackendPath>\jira_env\Scripts\python.exe.

.PARAMETER Node
    node.exe. Default: the one on the PATH.

.PARAMETER ApiPort
    The loopback port the API site listens on, and the dashboard calls. Default: 3300.

.PARAMETER TaskUser
    The account the scheduled jobs run as. Default: NT AUTHORITY\SYSTEM.

.PARAMETER SyncIntervalMinutes
    Minutes between two cycles, 5 to 1440. Default: 60.

.PARAMETER FullSyncAt
    The time of day (server time, HH:mm) of the nightly full sync. Default: 01:00.

.PARAMETER BackupAt
    The time of day (server time, HH:mm) of the daily backup. Default: 03:00.

.PARAMETER SkipTasks
    Leave Task Scheduler alone.

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy\iis\Install-JiraOversight.ps1 -HostName oversight.example.com -CertificateThumbprint 0123...ABCD -WhatIf
#>
#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)]
    [string] $HostName,
    [Parameter(Mandatory)]
    [string] $CertificateThumbprint,
    [string] $InstallRoot = '',
    [string] $BackendPath = '',
    [string] $DashboardPath = '',
    [string] $DashboardEnvFile = '',
    [string] $SitesPath = '',
    [string] $Python = '',
    [string] $Node = '',
    [ValidateRange(1024, 65535)]
    [int] $ApiPort = 3300,
    [string] $ApiSiteName = 'Jira Oversight API',
    [string] $DashboardSiteName = 'Jira Oversight',
    [string] $ApiPoolName = 'JiraOversightApi',
    [string] $DashboardPoolName = 'JiraOversightDashboard',
    [string] $TaskUser = 'NT AUTHORITY\SYSTEM',
    [ValidateRange(5, 1440)]
    [int] $SyncIntervalMinutes = 60,
    [string] $FullSyncAt = '01:00',
    [string] $BackupAt = '03:00',
    [switch] $SkipTasks
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$TaskFolder = '\JiraOversight\'
$WhatIfRun = [bool] $WhatIfPreference
$utf8 = New-Object System.Text.UTF8Encoding($false)
$changes = 0

function Write-Step([string] $Message) { Write-Host "[jira-oversight] $Message" }

function Stop-Or-Warn([string] $Message) {
    if ($WhatIfRun) { Write-Warning "$Message (a real run would stop here)" } else { throw $Message }
}

# --- paths ---------------------------------------------------------------------------------------------------------

function Resolve-Full([string] $Path) {
    if (-not [System.IO.Path]::IsPathRooted($Path)) { $Path = Join-Path (Get-Location).Path $Path }
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

if (-not $BackendPath) { $BackendPath = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
$BackendPath = Resolve-Full $BackendPath
if (-not $InstallRoot) { $InstallRoot = Split-Path -Parent $BackendPath }
$InstallRoot = Resolve-Full $InstallRoot
if (-not $DashboardPath) { $DashboardPath = Join-Path $InstallRoot 'dashboard' }
$DashboardPath = Resolve-Full $DashboardPath
if (-not $DashboardEnvFile) { $DashboardEnvFile = Join-Path $InstallRoot 'dashboard.env' }
$DashboardEnvFile = Resolve-Full $DashboardEnvFile
if (-not $SitesPath) { $SitesPath = Join-Path $InstallRoot 'sites' }
$SitesPath = Resolve-Full $SitesPath
if (-not $Python) { $Python = Join-Path $BackendPath 'jira_env\Scripts\python.exe' }
$Python = Resolve-Full $Python
if (-not $Node) {
    $found = Get-Command node.exe -ErrorAction SilentlyContinue
    $Node = if ($found) { $found.Source } else { Join-Path $env:ProgramFiles 'nodejs\node.exe' }
}
$Node = Resolve-Full $Node

$HostName = $HostName.Trim().ToLowerInvariant()
$thumbprint = ($CertificateThumbprint -replace '[^0-9A-Fa-f]', '').ToUpperInvariant()
$here = $PSScriptRoot
$apiSite = Join-Path $SitesPath 'api'
$dashboardSite = Join-Path $SitesPath 'dashboard'
$backendData = Join-Path $BackendPath 'data'
$backendLogs = Join-Path $BackendPath 'logs'
$backendReports = Join-Path $BackendPath 'reports'
$apiLogs = Join-Path $backendLogs 'api'
$dashboardLogs = Join-Path (Join-Path $InstallRoot 'logs') 'dashboard'
$backendEnvFile = Join-Path $BackendPath '.env'
$apiIdentity = "IIS AppPool\$ApiPoolName"
$dashboardIdentity = "IIS AppPool\$DashboardPoolName"
$taskRunsAsSystem = $TaskUser -match '^(NT AUTHORITY\\SYSTEM|SYSTEM)$'

# --- checks (they only read) -------------------------------------------------------------------------------------------

Write-Step "Backend: $BackendPath"
Write-Step "Dashboard: $DashboardPath (settings: $DashboardEnvFile)"
Write-Step "Sites: $SitesPath; address: https://$HostName; API: http://127.0.0.1:$ApiPort"
if ($WhatIfRun) { Write-Step 'WhatIf: nothing below is changed.' }

if ($HostName -notmatch '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$') { throw "-HostName must be a host name such as oversight.example.com, not '$HostName'." }
if ($thumbprint.Length -ne 40) { throw '-CertificateThumbprint must be the 40-character thumbprint of a certificate in Cert:\LocalMachine\My.' }
foreach ($time in @($FullSyncAt, $BackupAt)) {
    if ($time -notmatch '^([01][0-9]|2[0-3]):[0-5][0-9]$') { throw "A time of day must be HH:mm (24 hours), not '$time'." }
}
if (-not (Test-Path -LiteralPath (Join-Path $BackendPath 'oversight\__main__.py') -PathType Leaf)) {
    throw "No backend at $BackendPath (no oversight\__main__.py): pass -BackendPath."
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Stop-Or-Warn 'Run this in an elevated PowerShell (Run as administrator).'
}
foreach ($template in @('api\web.config', 'dashboard\web.config', 'backend.env.example', 'dashboard.env.example', 'Invoke-OversightTask.ps1')) {
    if (-not (Test-Path -LiteralPath (Join-Path $here $template) -PathType Leaf)) { throw "The pack is incomplete: $here\$template is missing." }
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    Stop-Or-Warn "No Python at ${Python}: create the virtual environment first (docs/DEPLOYMENT_IIS.md, section 4)."
}
if (-not (Test-Path -LiteralPath $Node -PathType Leaf)) { Stop-Or-Warn "No node.exe at ${Node}: install Node.js (section 3) or pass -Node." }
if (-not (Test-Path -LiteralPath (Join-Path $DashboardPath 'server.js') -PathType Leaf)) {
    Stop-Or-Warn "No dashboard at $DashboardPath (no server.js): run Publish-Dashboard.ps1 first (section 4)."
}
$certificate = Get-Item -LiteralPath "Cert:\LocalMachine\My\$thumbprint" -ErrorAction SilentlyContinue
if (-not $certificate) {
    Stop-Or-Warn "No certificate with thumbprint $thumbprint in Cert:\LocalMachine\My (section 9)."
} else {
    if ($certificate.NotAfter -lt (Get-Date)) { Stop-Or-Warn "The certificate expired on $($certificate.NotAfter.ToString('d MMM yyyy'))." }
    $names = @($certificate.DnsNameList | ForEach-Object { $_.Unicode.ToLowerInvariant() })
    $covered = $names | Where-Object { $_ -eq $HostName -or $_ -eq ($HostName -replace '^[^.]+', '*') }
    if (-not $covered) { Write-Warning "The certificate does not name $HostName (it names: $($names -join ', ')). Browsers will refuse it." }
}
if (Test-Path -LiteralPath $DashboardEnvFile -PathType Leaf) {
    $appUrl = Select-String -LiteralPath $DashboardEnvFile -Pattern '^\s*APP_URL\s*=\s*(.*)$' | Select-Object -First 1
    $appUrl = if ($appUrl) { $appUrl.Matches[0].Groups[1].Value.Trim().Trim('"', "'") } else { '' }
    if ($appUrl -and $appUrl.TrimEnd('/') -ne "https://$HostName") {
        Write-Warning "APP_URL in the dashboard's settings is not https://${HostName}: sign-in works only at the address APP_URL names."
    }
}

# IIS itself: read through the WebAdministration module. Without IIS a -WhatIf run still prints the plan.
$iisReady = $false
try {
    Import-Module WebAdministration -ErrorAction Stop
    $null = Get-Website -ErrorAction Stop
    $iisReady = $true
} catch {
    Stop-Or-Warn 'IIS and its PowerShell module (WebAdministration) are not installed: add the Web Server role with the IIS Management Scripts and Tools (section 3).'
}
if ($iisReady) {
    $modules = @(Get-WebGlobalModule -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    foreach ($needed in @(@('httpPlatformHandler', 'the HttpPlatformHandler module'), @('RewriteModule', 'the URL Rewrite module'))) {
        if ($modules -notcontains $needed[0]) { Stop-Or-Warn "IIS has no $($needed[1]) (section 3)." }
    }
}

# --- helpers that change things ----------------------------------------------------------------------------------------

function Initialize-Folder([string] $Path) {
    if (Test-Path -LiteralPath $Path -PathType Container) { return }
    if ($PSCmdlet.ShouldProcess($Path, 'create the folder')) {
        New-Item -ItemType Directory -Force -Path $Path | Out-Null
    }
    $script:changes++
}

# icacls /grant adds to what is there and is a no-op when the entry already exists; it never removes an entry.
function Grant-Access([string] $Path, [string] $Account, [string] $Rights, [string] $Why) {
    if ($PSCmdlet.ShouldProcess($Path, "grant $Account $Why")) {
        $grant = if (Test-Path -LiteralPath $Path -PathType Container) { "${Account}:(OI)(CI)$Rights" } else { "${Account}:$Rights" }
        $result = & icacls.exe $Path /grant $grant 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Could not grant $Account access to ${Path}: $result" }
    }
}

# The settings file's own list: administrators and SYSTEM in full, then only the accounts that read it. Not inherited.
function Protect-SettingsFile([string] $Path, [string[]] $Readers) {
    if ($PSCmdlet.ShouldProcess($Path, 'limit who can read it')) {
        $arguments = @($Path, '/inheritance:r', '/grant:r', '*S-1-5-32-544:(F)', '/grant:r', '*S-1-5-18:(F)')
        foreach ($reader in @($Readers | Where-Object { $_ })) { $arguments += @('/grant:r', "${reader}:(R)") }
        $result = & icacls.exe @arguments 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Could not limit access to ${Path}: $result" }
    }
}

function Copy-Template([string] $Template, [string] $Destination) {
    if (Test-Path -LiteralPath $Destination -PathType Leaf) { return $false }
    if ($PSCmdlet.ShouldProcess($Destination, "create it from $(Split-Path -Leaf $Template)")) {
        Copy-Item -LiteralPath $Template -Destination $Destination
    }
    $script:changes++
    return $true
}

function Write-WebConfig([string] $Template, [string] $Destination, [hashtable] $Values) {
    $text = [System.IO.File]::ReadAllText($Template)
    foreach ($name in $Values.Keys) {
        $text = $text.Replace("__${name}__", [System.Security.SecurityElement]::Escape([string] $Values[$name]))
    }
    $left = [regex]::Matches($text, '__[A-Z][A-Z_]*__') | ForEach-Object { $_.Value } | Sort-Object -Unique
    if ($left) { throw "$Template has placeholders this script does not fill: $($left -join ', ')" }
    [void] ([xml] $text)  # well-formed, or it stops here
    $current = if (Test-Path -LiteralPath $Destination -PathType Leaf) { [System.IO.File]::ReadAllText($Destination) } else { $null }
    if ($current -eq $text) { Write-Step "ok: $Destination"; return }
    if ($PSCmdlet.ShouldProcess($Destination, 'write web.config')) {
        [System.IO.File]::WriteAllText($Destination, $text, $utf8)
    }
    $script:changes++
}

function Get-IisValue([string] $Path, [string] $Name) {
    if (-not $iisReady) { return $null }
    try { return (Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction Stop) } catch { return $null }
}

function Set-IisValue([string] $Path, [string] $Name, $Value) {
    $current = Get-IisValue $Path $Name
    if ($null -ne $current -and "$current" -eq "$Value") { return }
    if ($PSCmdlet.ShouldProcess($Path, "set $Name to '$Value'")) {
        Set-ItemProperty -LiteralPath $Path -Name $Name -Value $Value
    }
    $script:changes++
}

function Initialize-Pool([string] $Name) {
    $path = "IIS:\AppPools\$Name"
    $exists = $iisReady -and (Test-Path -LiteralPath $path)
    if (-not $exists) {
        if ($PSCmdlet.ShouldProcess($Name, 'create the app pool')) { New-WebAppPool -Name $Name | Out-Null }
        $script:changes++
    }
    Set-IisValue $path 'managedRuntimeVersion' ''
    Set-IisValue $path 'managedPipelineMode' 'Integrated'
    Set-IisValue $path 'enable32BitAppOnWin64' $false
    Set-IisValue $path 'startMode' 'AlwaysRunning'
    Set-IisValue $path 'autoStart' $true
    Set-IisValue $path 'processModel.identityType' 'ApplicationPoolIdentity'
    Set-IisValue $path 'processModel.idleTimeout' ([TimeSpan]::Zero)
    Set-IisValue $path 'recycling.periodicRestart.time' ([TimeSpan]::Zero)
}

function Test-Binding([string] $Site, [string] $Protocol, [string] $Information) {
    if (-not $iisReady) { return $false }
    return [bool] (Get-WebBinding -Name $Site -Protocol $Protocol -ErrorAction SilentlyContinue | Where-Object { $_.bindingInformation -eq $Information })
}

function Initialize-Site([string] $Name, [string] $Pool, [string] $Folder, [string] $Protocol, [string] $Information, [string] $HostHeader, [string] $Address, [int] $Port) {
    $path = "IIS:\Sites\$Name"
    $exists = $iisReady -and (Test-Path -LiteralPath $path)
    if (-not $exists) {
        if ($PSCmdlet.ShouldProcess($Name, "create the site on $Information")) {
            $site = @{ Name = $Name; PhysicalPath = $Folder; ApplicationPool = $Pool; IPAddress = $Address; Port = $Port }
            if ($HostHeader) { $site.HostHeader = $HostHeader }
            New-Website @site | Out-Null
        }
        $script:changes++
        $script:newSites += $Name
    } else {
        Set-IisValue $path 'physicalPath' $Folder
        Set-IisValue $path 'applicationPool' $Pool
        if (-not (Test-Binding $Name $Protocol $Information)) {
            if ($PSCmdlet.ShouldProcess($Name, "add the binding $Information")) {
                $add = @{ Name = $Name; Protocol = $Protocol; IPAddress = $Address; Port = $Port }
                if ($HostHeader) { $add.HostHeader = $HostHeader }
                New-WebBinding @add
            }
            $script:changes++
        }
    }
    Set-IisValue $path 'applicationDefaults.preloadEnabled' $true
}

$newSites = @()

# --- 1. folders ------------------------------------------------------------------------------------------------------------

Write-Step '1. Folders'
foreach ($folder in @($backendData, $backendLogs, $backendReports, $apiLogs, (Join-Path $InstallRoot 'logs'), $dashboardLogs, $SitesPath, $apiSite, $dashboardSite, (Join-Path $DashboardPath '.next\cache'))) {
    Initialize-Folder $folder
}

# --- 2. the settings files -------------------------------------------------------------------------------------------------

Write-Step '2. Settings files'
$madeSettings = $false
if (Copy-Template (Join-Path $here 'backend.env.example') $backendEnvFile) { $madeSettings = $true; Write-Step "created ${backendEnvFile}: fill it in (section 7)" }
if (Copy-Template (Join-Path $here 'dashboard.env.example') $DashboardEnvFile) { $madeSettings = $true; Write-Step "created ${DashboardEnvFile}: fill it in (section 7)" }

# --- 3. app pools ----------------------------------------------------------------------------------------------------------

Write-Step '3. App pools'
Initialize-Pool $ApiPoolName
Initialize-Pool $DashboardPoolName

# --- 4. web.config files and sites -----------------------------------------------------------------------------------------

Write-Step '4. Sites'
$values = @{
    PYTHON = $Python
    BACKEND = $BackendPath
    API_LOGS = $apiLogs
    NODE = $Node
    DASHBOARD_APP = $DashboardPath
    DASHBOARD_ENV = $DashboardEnvFile
    DASHBOARD_LOGS = $dashboardLogs
    API_PORT = "$ApiPort"
    HOST_NAME = $HostName
}
Write-WebConfig (Join-Path $here 'api\web.config') (Join-Path $apiSite 'web.config') $values
Write-WebConfig (Join-Path $here 'dashboard\web.config') (Join-Path $dashboardSite 'web.config') $values

Initialize-Site $ApiSiteName $ApiPoolName $apiSite 'http' "127.0.0.1:${ApiPort}:" '' '127.0.0.1' $ApiPort
Initialize-Site $DashboardSiteName $DashboardPoolName $dashboardSite 'http' "*:80:$HostName" $HostName '*' 80

# https on the address, with the certificate (SNI, so the host name is part of the binding).
$httpsInformation = "*:443:$HostName"
if (-not (Test-Binding $DashboardSiteName 'https' $httpsInformation)) {
    if ($PSCmdlet.ShouldProcess($DashboardSiteName, "add the https binding $httpsInformation")) {
        New-WebBinding -Name $DashboardSiteName -Protocol https -IPAddress '*' -Port 443 -HostHeader $HostName -SslFlags 1
    }
    $changes++
}
$binding = if ($iisReady) { Get-WebBinding -Name $DashboardSiteName -Protocol https -HostHeader $HostName -ErrorAction SilentlyContinue | Select-Object -First 1 } else { $null }
$bound = if ($binding) { [string] $binding.certificateHash } else { '' }
if ($bound -ne $thumbprint) {
    if ($PSCmdlet.ShouldProcess($DashboardSiteName, "bind the certificate $thumbprint to $httpsInformation")) {
        if ($bound) { $binding.RemoveSslCertificate() }
        $binding.AddSslCertificate($thumbprint, 'My')
    }
    $changes++
}

# --- 5. folder permissions -------------------------------------------------------------------------------------------------

Write-Step '5. Folder permissions'
# The API: reads the backend (code, config, the venv, .env), writes the snapshot and its backups, the logs and the reports.
Grant-Access $BackendPath $apiIdentity 'RX' 'read and run the backend'
foreach ($folder in @($backendData, $backendLogs, $backendReports)) {
    Grant-Access $folder $apiIdentity 'M' 'change'
}
Grant-Access $apiSite $apiIdentity 'RX' 'read its site'
# The dashboard: reads its server and its own site, writes its stdout log and Next.js's cache.
Grant-Access $DashboardPath $dashboardIdentity 'RX' 'read and run the dashboard'
Grant-Access (Join-Path $DashboardPath '.next\cache') $dashboardIdentity 'M' 'change'
Grant-Access $dashboardLogs $dashboardIdentity 'M' 'change'
Grant-Access $dashboardSite $dashboardIdentity 'RX' 'read its site'
# The scheduled jobs: the same as the API, as the job's own account.
if (-not $taskRunsAsSystem -and -not $SkipTasks) {
    Grant-Access $BackendPath $TaskUser 'RX' 'read and run the backend'
    foreach ($folder in @($backendData, $backendLogs, $backendReports)) {
        Grant-Access $folder $TaskUser 'M' 'change'
    }
    # `doctor` (run by an administrator) writes config\fields.yaml; the jobs only read it.
}
# The settings files: not inherited, and readable by the account that needs each.
if (Test-Path -LiteralPath $backendEnvFile) {
    Protect-SettingsFile $backendEnvFile @($apiIdentity, $(if ($taskRunsAsSystem) { $null } else { $TaskUser }))
} elseif (-not $WhatIfRun) { throw "No settings file at $backendEnvFile." }
if (Test-Path -LiteralPath $DashboardEnvFile) {
    Protect-SettingsFile $DashboardEnvFile @($dashboardIdentity)
} elseif (-not $WhatIfRun) { throw "No settings file at $DashboardEnvFile." }

# --- 6. scheduled jobs -----------------------------------------------------------------------------------------------------

if ($SkipTasks) {
    Write-Step '6. Scheduled jobs: skipped (-SkipTasks)'
} else {
    Write-Step '6. Scheduled jobs'
    $wrapper = Join-Path $here 'Invoke-OversightTask.ps1'
    $prune = "$apiLogs;$dashboardLogs"
    $principal = New-ScheduledTaskPrincipal -UserId $TaskUser -LogonType ServiceAccount
    # One run at a time per job, a missed run starts when the machine is back, and a job that hangs is ended.
    $limit = New-TimeSpan -Hours 4
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit $limit
    $today = (Get-Date).Date
    $jobs = @(
        @{
            Name = 'Sync'; Task = 'Cycle'
            Trigger = New-ScheduledTaskTrigger -Once -At $today.AddMinutes(15) -RepetitionInterval (New-TimeSpan -Minutes $SyncIntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
            About = "Every $SyncIntervalMinutes minutes: board discovery, then sync, derive and rules. Jira is only read."
        },
        @{
            Name = 'Full sync'; Task = 'FullCycle'
            Trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($FullSyncAt, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture))
            About = 'Once a night: reads every issue, so issues deleted or moved in Jira are noticed.'
        },
        @{
            Name = 'Backup'; Task = 'Backup'
            Trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($BackupAt, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture))
            About = 'Once a day: a checked copy of the snapshot in data\backups, and old logs deleted.'
        }
    )
    foreach ($job in $jobs) {
        $argument = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Task {1} -Python "{2}" -PruneDir "{3}"' -f $wrapper, $job.Task, $Python, $prune
        $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argument -WorkingDirectory $BackendPath
        $present = Get-ScheduledTask -TaskPath $TaskFolder -TaskName $job.Name -ErrorAction SilentlyContinue
        $verb = if ($present) { 'replace' } else { 'register' }
        if ($PSCmdlet.ShouldProcess("$TaskFolder$($job.Name)", "$verb the scheduled job (as $TaskUser)")) {
            Register-ScheduledTask -TaskPath $TaskFolder -TaskName $job.Name -Action $action -Trigger $job.Trigger -Settings $settings -Principal $principal -Description "Jira Oversight. $($job.About)" -Force | Out-Null
        }
        $changes++
    }
}

# --- 7. a new site waits for its settings ----------------------------------------------------------------------------------

if ($madeSettings -and -not $WhatIfRun) {
    foreach ($site in $newSites) {
        Stop-Website -Name $site -ErrorAction SilentlyContinue
    }
    if ($newSites) { Write-Step "left stopped until the settings are filled in: $($newSites -join ', ')" }
}

$count = if ($changes -eq 0) { 'nothing to change' } else { '{0} change{1}{2}' -f $changes, $(if ($changes -eq 1) { '' } else { 's' }), $(if ($WhatIfRun) { ' would be made' } else { ' made' }) }
Write-Step ('Done{0}: {1}.' -f $(if ($WhatIfRun) { ' (WhatIf, nothing was changed)' } else { '' }), $count)
if ($madeSettings) {
    Write-Step 'Next: fill in both settings files, run the doctor (section 10), then start the sites.'
}
