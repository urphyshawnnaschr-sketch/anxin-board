[CmdletBinding()]
param([string]$TestInstallRoot)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw 'LOCALAPPDATA is unavailable.'
}
if (-not [string]::IsNullOrWhiteSpace($TestInstallRoot) -and $env:ANXINBOARD_INSTALLER_TEST_MODE -ne '1') {
    throw 'TestInstallRoot is available only in explicit installer test mode.'
}

$stateDir = Join-Path $env:LOCALAPPDATA 'AnxinBoard'
$runStatePath = Join-Path $stateDir 'installed-run.json'
$launchLockPath = Join-Path $stateDir 'secure-launch.lock'
$installRoot = if ([string]::IsNullOrWhiteSpace($TestInstallRoot)) {
    Join-Path $env:LOCALAPPDATA 'Programs\AnxinBoard'
} else {
    [System.IO.Path]::GetFullPath($TestInstallRoot)
}
$installRoot = [System.IO.Path]::GetFullPath($installRoot)
$launchLock = $null

function Assert-RecordedRuntimePath {
    param([Parameter(Mandatory = $true)]$State)

    $recordedRoot = [System.IO.Path]::GetFullPath([string]$State.install_root)
    if (-not $recordedRoot.Equals($installRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installed runtime state belongs to a different install root.'
    }
    if ([string]$State.source_commit -notmatch '^[0-9a-f]{40}$') {
        throw 'Installed runtime state source commit is invalid.'
    }
    if ([string]$State.payload_digest -notmatch '^[0-9a-f]{64}$') {
        throw 'Installed runtime state payload digest is invalid.'
    }

    $runtimePath = [System.IO.Path]::GetFullPath([string]$State.runtime_executable)
    $rootPrefix = $installRoot.TrimEnd('\') + '\'
    if (-not $runtimePath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installed runtime state executable escapes the product root.'
    }
    if (-not (Test-Path -LiteralPath $runtimePath -PathType Leaf)) {
        throw 'Installed runtime state executable is missing; refusing to identify a live process.'
    }
    $item = Get-Item -LiteralPath $runtimePath -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Installed runtime state executable is a reparse point.'
    }
    return $runtimePath
}

New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
try {
    try {
        $launchLock = [System.IO.File]::Open(
            $launchLockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        throw 'Another AnxinBoard lifecycle operation is already running.'
    }

    if (-not (Test-Path -LiteralPath $runStatePath -PathType Leaf)) {
        Write-Host 'ANXIN_INSTALLED_RUNTIME=ALREADY_STOPPED'
        exit 0
    }

    try {
        $stateItem = Get-Item -LiteralPath $runStatePath -Force
        if (($stateItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Installed runtime state is not a plain file.'
        }
        $state = Get-Content -LiteralPath $runStatePath -Raw -Encoding utf8 | ConvertFrom-Json
    } catch {
        throw 'Installed runtime state is unreadable; refusing to stop an unidentified process.'
    }
    if ($state.schema_version -ne 'anxin_installed_run_state_v1') {
        throw 'Installed runtime state schema is unsupported.'
    }
    $recordedRuntime = Assert-RecordedRuntimePath -State $state

    $pidValue = [int]$state.pid
    if ($pidValue -le 0) { throw 'Installed runtime state PID is invalid.' }
    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $runStatePath -Force
        Write-Host 'ANXIN_INSTALLED_RUNTIME=STALE_STATE_CLEARED'
        exit 0
    }

    $sameName = ([string]$process.ProcessName -eq [string]$state.process_name)
    $sameStart = $false
    try {
        $recordedStart = [DateTime]::Parse([string]$state.start_time)
        $sameStart = ([Math]::Abs(($process.StartTime - $recordedStart).TotalSeconds) -lt 1)
    } catch { $sameStart = $false }
    if (-not ($sameName -and $sameStart)) {
        throw 'Installed runtime state no longer identifies the live process; refusing to terminate it.'
    }

    $processPath = [string]$process.Path
    if ([string]::IsNullOrWhiteSpace($processPath)) {
        throw 'Installed runtime process path is unavailable; refusing to terminate it.'
    }
    $processPath = [System.IO.Path]::GetFullPath($processPath)
    if (-not $processPath.Equals($recordedRuntime, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installed runtime state points at a different executable; refusing to terminate it.'
    }

    Stop-Process -Id $pidValue -Force -ErrorAction Stop
    try { [void]$process.WaitForExit(5000) } catch { }
    if (-not $process.HasExited) { throw 'Installed runtime did not stop.' }
    Remove-Item -LiteralPath $runStatePath -Force
    Write-Host 'ANXIN_INSTALLED_RUNTIME=STOPPED'
} finally {
    if ($null -ne $launchLock) {
        try { $launchLock.Dispose() } catch { }
    }
}
