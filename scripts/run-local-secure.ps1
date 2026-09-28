# AnxinBoard Windows guarded browser-session launcher wrapper.
# Generates one process-local bootstrap secret, starts the existing loopback runtime,
# then opens the browser exactly once with the bootstrap inside the URL fragment.
# Fragments are not sent in HTTP requests; the frontend exchanges the bootstrap for an
# in-memory session and immediately removes it from the address bar.

param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$stateDir = Join-Path $env:LOCALAPPDATA 'AnxinBoard'
$stateFile = Join-Path $stateDir 'local-run.json'
$launchLockFile = Join-Path $stateDir 'secure-launch.lock'
$runLocal = Join-Path $PSScriptRoot 'run-local.ps1'
$bootstrapEnv = 'ANXINBOARD_LOCAL_BOOTSTRAP_SECRET'
$launchLock = $null
$bootstrap = $null

function New-LauncherBootstrap {
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

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    Write-Host '启动失败：Windows LOCALAPPDATA 不可用。'
    exit 1
}

try {
    # File sharing is machine/filesystem scoped rather than Terminal Services session scoped.
    # Holding one FileShare.None handle under this user's LOCALAPPDATA therefore serializes
    # console/RDP/fast-user sessions that share the same durable local-run.json authority.
    # A crashed launcher automatically loses the OS file handle; the empty lock file may
    # remain, but a later launcher can acquire it and must still pass run-local's state check.
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    try {
        $launchLock = [System.IO.File]::Open(
            $launchLockFile,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        Write-Host '启动失败：另一个安心看板安全启动流程正在进行；本次不会再启动第二套本地运行时。'
        exit 1
    }

    # The cross-session file lock is acquired before bootstrap creation and before run-local.
    $bootstrap = New-LauncherBootstrap
    $oldBootstrap = [Environment]::GetEnvironmentVariable($bootstrapEnv, 'Process')
    try {
        [Environment]::SetEnvironmentVariable($bootstrapEnv, $bootstrap, 'Process')
        & $runLocal -NoBrowser
        $runExit = $LASTEXITCODE
    } finally {
        [Environment]::SetEnvironmentVariable($bootstrapEnv, $oldBootstrap, 'Process')
    }

    if ($runExit -ne 0) {
        exit $runExit
    }

    if ($NoBrowser) {
        Write-Host '真实 AI 发送授权保持关闭：NoBrowser 模式不会建立浏览器 Human session。'
        exit 0
    }

    try {
        if (-not (Test-Path $stateFile -PathType Leaf)) {
            throw "启动状态文件不存在：$stateFile"
        }
        $state = Get-Content -Path $stateFile -Raw -Encoding utf8 | ConvertFrom-Json
        $frontendPort = [int]$state.frontendPort
        if ($frontendPort -lt 1 -or $frontendPort -gt 65535) {
            throw 'frontendPort 无效。'
        }
        $escaped = [Uri]::EscapeDataString($bootstrap)
        $browserUrl = "http://127.0.0.1:$frontendPort/#/projects?anxin_bootstrap=$escaped"
        Start-Process $browserUrl | Out-Null
        Write-Host '      已建立一次性本地浏览器会话；bootstrap 不会进入 HTTP 请求、项目文件或运行状态文件。'
    } catch {
        Write-Host "      浏览器安全会话未能自动建立：$($_.Exception.Message)"
        Write-Host '      软件仍已启动，但真实 AI 发送授权保持关闭。'
    }
} finally {
    $bootstrap = $null
    if ($null -ne $launchLock) {
        try { $launchLock.Dispose() } catch { }
    }
}

exit 0
