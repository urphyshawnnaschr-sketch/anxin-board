[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [string]$TestInstallRoot
)

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
$resolverPath = Join-Path $PSScriptRoot 'resolve-installed-runtime.ps1'
$installRoot = if ([string]::IsNullOrWhiteSpace($TestInstallRoot)) {
    Join-Path $env:LOCALAPPDATA 'Programs\AnxinBoard'
} else {
    [System.IO.Path]::GetFullPath($TestInstallRoot)
}
$installRoot = [System.IO.Path]::GetFullPath($installRoot)
$launchLock = $null
$runtimeProcess = $null
$stateWritten = $false
$bootstrap = $null
$bootstrapEnv = 'ANXINBOARD_LOCAL_BOOTSTRAP_SECRET'

function New-BootstrapSecret {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
        return [Convert]::ToBase64String($bytes)
    } finally {
        $rng.Dispose()
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
}

function Request-LocalSessionHandoff {
    param([int]$RuntimePid)
    # No HTTP mint endpoint or persistent secret. Validate the connected server PID
    # before sending the fixed request; the runtime restricts the pipe to this user.
    try {
        if (-not ('AnxinBoard.LauncherHandoff' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using Microsoft.Win32.SafeHandles;
namespace AnxinBoard {
    public static class LauncherHandoff {
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern bool GetNamedPipeServerProcessId(SafePipeHandle pipe, out uint pid);
        public static string Request(int runtimePid) {
            using (var pipe = new NamedPipeClientStream(".", "AnxinBoard.LocalSession." + runtimePid,
                PipeDirection.InOut, PipeOptions.Asynchronous, TokenImpersonationLevel.Anonymous)) {
                pipe.Connect(3000);
                uint serverPid;
                if (!GetNamedPipeServerProcessId(pipe.SafePipeHandle, out serverPid) || serverPid != runtimePid)
                    throw new IOException("HANDOFF_SERVER_IDENTITY_INVALID");
                byte[] request = Encoding.ASCII.GetBytes("MINT\n");
                var write = pipe.WriteAsync(request, 0, request.Length);
                if (!write.Wait(3000)) throw new IOException("HANDOFF_TIMEOUT");
                byte[] response = new byte[257];
                int count = 0;
                var deadline = DateTime.UtcNow.AddSeconds(3);
                while (count < response.Length && DateTime.UtcNow < deadline) {
                    var read = pipe.ReadAsync(response, count, 1);
                    int remaining = Math.Max(1, (int)(deadline - DateTime.UtcNow).TotalMilliseconds);
                    if (!read.Wait(remaining)) throw new IOException("HANDOFF_TIMEOUT");
                    if (read.Result != 1) throw new IOException("HANDOFF_INVALID");
                    if (response[count++] == 10) {
                        string token = Encoding.ASCII.GetString(response, 0, count - 1);
                        if (!System.Text.RegularExpressions.Regex.IsMatch(token, "^[A-Za-z0-9_-]{43,128}$"))
                            throw new IOException("HANDOFF_INVALID");
                        Array.Clear(response, 0, response.Length);
                        return token;
                    }
                }
                throw new IOException("HANDOFF_INVALID");
            }
        }
    }
}
'@
        }
        return [AnxinBoard.LauncherHandoff]::Request($RuntimePid)
    } catch {
        throw 'LOCAL_SESSION_HANDOFF_UNAVAILABLE: unable to authorize this browser; the running service was not restarted.'
    }
}

function Get-AvailableLoopbackPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return [int]([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally {
        $listener.Stop()
    }
}

function Wait-HealthyRuntime {
    param([Parameter(Mandatory = $true)]$Process, [Parameter(Mandatory = $true)][string]$Origin)
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) {
            throw "Installed runtime exited before health check with code $($Process.ExitCode)."
        }
        try {
            $health = Invoke-RestMethod -Uri "$Origin/api/health" -Method Get -TimeoutSec 2
            if ($health.status -eq 'ok') { return }
        } catch {
            Start-Sleep -Milliseconds 400
        }
    }
    throw 'Installed runtime did not become healthy within 45 seconds.'
}

function Read-ExistingRunState {
    if (-not (Test-Path -LiteralPath $runStatePath -PathType Leaf)) { return $null }
    try {
        $item = Get-Item -LiteralPath $runStatePath -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Existing installed runtime state is not a plain file.'
        }
        $state = Get-Content -LiteralPath $runStatePath -Raw -Encoding utf8 | ConvertFrom-Json
    } catch {
        throw 'Existing installed runtime state is unreadable; refusing to overwrite it.'
    }
    if ($state.schema_version -ne 'anxin_installed_run_state_v1') {
        throw 'Existing installed runtime state schema is unsupported.'
    }
    return $state
}

function Resolve-CurrentRuntime {
    if (-not (Test-Path -LiteralPath $resolverPath -PathType Leaf)) {
        throw 'Installed runtime resolver is missing.'
    }
    $resolveArgs = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $resolverPath)
    if (-not [string]::IsNullOrWhiteSpace($TestInstallRoot)) {
        $resolveArgs += @('-TestInstallRoot', $TestInstallRoot)
    }
    $resolutionJson = & powershell.exe @resolveArgs
    if ($LASTEXITCODE -ne 0) { throw 'Installed runtime resolution failed.' }
    $resolution = ($resolutionJson -join "`n") | ConvertFrom-Json
    if ($resolution.schema_version -ne 'anxin_installed_runtime_resolution_v1') {
        throw 'Installed runtime resolution schema is unsupported.'
    }
    if ([string]$resolution.payload_digest -notmatch '^[0-9a-f]{64}$') {
        throw 'Installed runtime payload identity is invalid.'
    }
    if ([string]$resolution.source_commit -notmatch '^[0-9a-f]{40}$') {
        throw 'Installed runtime source identity is invalid.'
    }

    $runtimeRelative = ([string]$resolution.runtime_executable_relative).Replace('/', '\')
    if ([string]::IsNullOrWhiteSpace($runtimeRelative) -or [System.IO.Path]::IsPathRooted($runtimeRelative)) {
        throw 'Installed runtime executable binding is invalid.'
    }
    if ($runtimeRelative.Split('\') | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' }) {
        throw 'Installed runtime executable binding is invalid.'
    }
    $installRootFull = $installRoot.TrimEnd('\') + '\'
    $runtimeExe = [System.IO.Path]::GetFullPath((Join-Path $installRoot $runtimeRelative))
    if (-not $runtimeExe.StartsWith($installRootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installed runtime executable escapes the product root.'
    }
    if (-not (Test-Path -LiteralPath $runtimeExe -PathType Leaf)) {
        throw 'Installed runtime executable is missing.'
    }
    $runtimeItem = Get-Item -LiteralPath $runtimeExe -Force
    if (($runtimeItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Installed runtime executable is a reparse point.'
    }

    return [pscustomobject]@{
        Resolution = $resolution
        RuntimeExecutable = $runtimeExe
    }
}

function Get-LoopbackOriginPort {
    param([Parameter(Mandatory = $true)][string]$Origin)
    $match = [regex]::Match($Origin, '^http://127\.0\.0\.1:(?<port>[1-9][0-9]{0,4})$')
    if (-not $match.Success) {
        throw 'Installed runtime state origin is not an exact loopback HTTP origin.'
    }
    $port = [int]$match.Groups['port'].Value
    if ($port -lt 1 -or $port -gt 65535) {
        throw 'Installed runtime state origin port is invalid.'
    }
    return $port
}

function Assert-LoopbackOrigin {
    param([Parameter(Mandatory = $true)][string]$Origin)
    [void](Get-LoopbackOriginPort -Origin $Origin)
    return $Origin
}

function Assert-LoopbackListenerOwnership {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][string]$Origin
    )
    $port = Get-LoopbackOriginPort -Origin $Origin
    try {
        $listeners = @(Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $port -State Listen -ErrorAction Stop)
    } catch {
        throw 'Unable to verify installed runtime loopback listener ownership.'
    }
    if ($listeners.Count -ne 1) {
        throw 'Installed runtime loopback listener identity is ambiguous.'
    }
    if ([int]$listeners[0].OwningProcess -ne [int]$Process.Id) {
        throw 'Installed runtime loopback listener is owned by a different process.'
    }
}

function Assert-ExistingRuntimeBinding {
    param(
        [Parameter(Mandatory = $true)]$State,
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)]$Resolved
    )

    $recordedRoot = [System.IO.Path]::GetFullPath([string]$State.install_root)
    if (-not $recordedRoot.Equals($installRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installed runtime state belongs to a different install root.'
    }
    if ([string]$State.source_commit -cne [string]$Resolved.Resolution.source_commit) {
        throw 'Installed runtime state source commit no longer matches the active payload.'
    }
    if ([string]$State.payload_digest -cne [string]$Resolved.Resolution.payload_digest) {
        throw 'Installed runtime state payload digest no longer matches the active payload.'
    }

    $recordedRuntime = [System.IO.Path]::GetFullPath([string]$State.runtime_executable)
    if (-not $recordedRuntime.Equals([string]$Resolved.RuntimeExecutable, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installed runtime state executable no longer matches the active payload.'
    }
    $processPath = [string]$Process.Path
    if ([string]::IsNullOrWhiteSpace($processPath)) {
        throw 'Installed runtime process path is unavailable.'
    }
    $processPath = [System.IO.Path]::GetFullPath($processPath)
    if (-not $processPath.Equals([string]$Resolved.RuntimeExecutable, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Installed runtime state points at a different executable; refusing reuse.'
    }
    return Assert-LoopbackOrigin -Origin ([string]$State.origin)
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

    # Resolve and integrity-check the active payload before either reusing an existing
    # process or starting a new one. A stale/corrupt current pointer therefore fails closed.
    $resolved = Resolve-CurrentRuntime
    $resolution = $resolved.Resolution
    $runtimeExe = [string]$resolved.RuntimeExecutable

    $existing = Read-ExistingRunState
    if ($null -ne $existing) {
        $existingPid = [int]$existing.pid
        if ($existingPid -le 0) { throw 'Existing installed runtime state PID is invalid.' }
        $existingProcess = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($null -ne $existingProcess) {
            $sameName = ([string]$existingProcess.ProcessName -eq [string]$existing.process_name)
            $sameStart = $false
            try {
                $recordedStart = [DateTime]::Parse([string]$existing.start_time)
                $sameStart = ([Math]::Abs(($existingProcess.StartTime - $recordedStart).TotalSeconds) -lt 1)
            } catch { $sameStart = $false }
            if (-not ($sameName -and $sameStart)) {
                throw 'Installed runtime state points at a different live process; refusing to replace it.'
            }
            $existingOrigin = Assert-ExistingRuntimeBinding -State $existing -Process $existingProcess -Resolved $resolved
            Wait-HealthyRuntime -Process $existingProcess -Origin $existingOrigin
            Assert-LoopbackListenerOwnership -Process $existingProcess -Origin $existingOrigin
            if (-not $NoBrowser) {
                $bootstrap = Request-LocalSessionHandoff -RuntimePid $existingPid
                $escaped = [Uri]::EscapeDataString($bootstrap)
                Start-Process "$existingOrigin/#/projects?anxin_bootstrap=$escaped" | Out-Null
            }
            Write-Host 'ANXIN_INSTALLED_RUNTIME=ALREADY_RUNNING'
            Write-Host "ORIGIN=$existingOrigin"
            exit 0
        }
        Remove-Item -LiteralPath $runStatePath -Force
    }

    $port = Get-AvailableLoopbackPort
    $origin = "http://127.0.0.1:$port"
    [void](Assert-LoopbackOrigin -Origin $origin)
    $bootstrap = New-BootstrapSecret
    $oldBootstrap = [Environment]::GetEnvironmentVariable($bootstrapEnv, 'Process')
    try {
        [Environment]::SetEnvironmentVariable($bootstrapEnv, $bootstrap, 'Process')
        $runtimeProcess = Start-Process -FilePath $runtimeExe -ArgumentList @('--port', "$port") -WorkingDirectory (Split-Path -Parent $runtimeExe) -PassThru -WindowStyle Hidden
    } finally {
        [Environment]::SetEnvironmentVariable($bootstrapEnv, $oldBootstrap, 'Process')
    }

    Wait-HealthyRuntime -Process $runtimeProcess -Origin $origin
    Assert-LoopbackListenerOwnership -Process $runtimeProcess -Origin $origin
    $runState = [ordered]@{
        schema_version = 'anxin_installed_run_state_v1'
        pid = [int]$runtimeProcess.Id
        process_name = [string]$runtimeProcess.ProcessName
        start_time = $runtimeProcess.StartTime.ToString('o')
        origin = $origin
        payload_digest = [string]$resolution.payload_digest
        source_commit = [string]$resolution.source_commit
        runtime_executable = $runtimeExe
        install_root = $installRoot
        started_at = [DateTimeOffset]::UtcNow.ToString('o')
    }
    $tempState = "$runStatePath.tmp-$([Guid]::NewGuid().ToString('N'))"
    try {
        $runState | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $tempState -Encoding utf8
        Move-Item -LiteralPath $tempState -Destination $runStatePath -Force
        $stateWritten = $true
    } finally {
        if (Test-Path -LiteralPath $tempState) { Remove-Item -LiteralPath $tempState -Force -ErrorAction SilentlyContinue }
    }

    if (-not $NoBrowser) {
        $escaped = [Uri]::EscapeDataString($bootstrap)
        Start-Process "$origin/#/projects?anxin_bootstrap=$escaped" | Out-Null
    }
    Write-Host 'ANXIN_INSTALLED_RUNTIME=STARTED'
    Write-Host "ORIGIN=$origin"
    Write-Host "PID=$($runtimeProcess.Id)"
} catch {
    if ($runtimeProcess -and -not $runtimeProcess.HasExited) {
        Stop-Process -Id $runtimeProcess.Id -Force -ErrorAction SilentlyContinue
        try { [void]$runtimeProcess.WaitForExit(5000) } catch { }
    }
    if ($stateWritten -and (Test-Path -LiteralPath $runStatePath)) {
        Remove-Item -LiteralPath $runStatePath -Force -ErrorAction SilentlyContinue
    }
    throw
} finally {
    $bootstrap = $null
    if ($null -ne $launchLock) {
        try { $launchLock.Dispose() } catch { }
    }
}
