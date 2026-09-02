<#
.SYNOPSIS
    Keeps the Hermes gateway alive so scheduled cron jobs actually fire.

.DESCRIPTION
    Hermes cron only runs while the gateway process is up. The gateway is
    normally launched attached to a terminal or the desktop app rather than as
    a service, so it dies on logout, sleep, or a crash -- gateway-exit-diag.log
    recorded unclean exits on six days across Aug 13-19 and several times a day
    by Sep 1-2. A dead gateway is silent: the 09:00 run on 2026-08-19 simply
    never happened, and the nightly watchdog cannot catch it because it needs
    the same gateway to run itself.

    This script is the missing supervisor. Registered as a Scheduled Task that
    fires at logon and then every few minutes, it starts the gateway whenever
    it is not running and otherwise does nothing.

    Deliberately conservative: it only ever starts a gateway when NO hermes
    process is running, so it cannot spawn a competing second instance. It
    never kills anything.

.PARAMETER WhatIf
    Report what would happen without starting anything.

.NOTES
    Escape hatch: create the file gateway-guard.disabled in the profile
    directory and this script does nothing until it is removed. Use that
    instead of deleting the task when stopping Hermes deliberately.
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'

$LocalAppData = $env:LOCALAPPDATA
if (-not $LocalAppData) { $LocalAppData = Join-Path $env:USERPROFILE 'AppData\Local' }

$HermesRoot  = Join-Path $LocalAppData 'hermes'
$ActiveFile  = Join-Path $HermesRoot 'active_profile'
$Profile     = if (Test-Path $ActiveFile) { (Get-Content $ActiveFile -Raw).Trim() } else { 'project-manager' }
$ProfileDir  = Join-Path $HermesRoot "profiles\$Profile"
$HermesExe   = Join-Path $HermesRoot 'hermes-agent\venv\Scripts\hermes.exe'
$DisableFlag = Join-Path $ProfileDir 'gateway-guard.disabled'
$LogFile     = Join-Path $ProfileDir 'logs\gateway-guard.log'

function Write-GuardLog([string]$Message) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    try {
        $dir = Split-Path $LogFile -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    } catch { }
    Write-Output $line
}

if (Test-Path $DisableFlag) {
    Write-GuardLog "held: gateway-guard.disabled present, doing nothing"
    exit 0
}

if (-not (Test-Path $HermesExe)) {
    Write-GuardLog "ERROR: hermes.exe not found at $HermesExe"
    exit 1
}

# Is a gateway already up?
#
# Match narrowly. Testing "commandline contains hermes and gateway" matched
# git.exe and this script's own powershell.exe, because unrelated tooling
# mentions those words in its arguments -- a guard that always believes the
# gateway is up would never restart it, which is worse than no guard at all.
# The real process is a python.exe (or the hermes.exe launcher) running the
# literal argument pair "gateway run".
$gatewayHosts = @('python.exe', 'pythonw.exe', 'hermes.exe')
$running = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $gatewayHosts -contains $_.Name -and
        $_.CommandLine -and
        $_.CommandLine -match 'hermes' -and
        $_.CommandLine -match 'gateway\s+run'
    })

if ($running.Count -gt 0) {
    $pids = ($running | ForEach-Object { $_.ProcessId }) -join ', '
    Write-Verbose "gateway already running (pid $pids)"
    exit 0
}

# Cross-check the scheduler heartbeat purely for the log line, so a restart
# record says how long cron had actually been dead.
$staleFor = 'unknown'
$hb = Join-Path $ProfileDir 'cron\ticker_heartbeat'
if (Test-Path $hb) {
    try {
        $epoch = [double](Get-Content $hb -Raw).Trim()
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $epoch
        $staleFor = "{0:N0}s" -f $age
    } catch { }
}

if ($PSCmdlet.ShouldProcess('hermes gateway', 'start')) {
    try {
        Start-Process -FilePath $HermesExe -ArgumentList 'gateway', 'run' `
            -WindowStyle Hidden -WorkingDirectory $HermesRoot | Out-Null
        Write-GuardLog "started gateway (was down; scheduler heartbeat stale $staleFor)"
    } catch {
        Write-GuardLog "ERROR: failed to start gateway: $($_.Exception.Message)"
        exit 1
    }
} else {
    Write-GuardLog "WhatIf: would start gateway (heartbeat stale $staleFor)"
}

exit 0
