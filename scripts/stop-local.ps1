# 停止 scripts/run-local.ps1 启动的 AnxinBoard 本地开发进程。
# 只依据 %LOCALAPPDATA%\AnxinBoard\local-run.json 中的记录操作，
# 且必须同时核对仓库路径、PID、进程名和启动时间；不会仅凭端口号终止任何进程。
#
# 用法（仓库根目录）：
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-local.ps1

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path (Join-Path $env:LOCALAPPDATA 'AnxinBoard') 'local-run.json'

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    Write-Host '停止失败：Windows LOCALAPPDATA 不可用，无法定位本项目运行状态；不终止任何进程。'
    exit 1
}

if (-not (Test-Path $stateFile -PathType Leaf)) {
    Write-Host "未找到本地启动记录：$stateFile"
    Write-Host '没有可安全确认属于本次本地启动器的进程，因此不终止任何进程。'
    exit 1
}

try {
    $state = Get-Content -Path $stateFile -Raw | ConvertFrom-Json
} catch {
    Write-Host "状态文件无法解析：$($_.Exception.Message)"
    Write-Host '为避免误杀进程，本次不做任何终止操作。'
    exit 1
}

if ([string]::IsNullOrWhiteSpace([string]$state.repoRoot) -or $state.repoRoot -ne $repoRoot) {
    Write-Host "状态文件记录的仓库路径（$($state.repoRoot)）与当前仓库（$repoRoot）不一致。"
    Write-Host '为避免误杀进程，本次不做任何终止操作。'
    exit 1
}

$identityFailed = $false
$killFailed = @()

foreach ($entry in @($state.processes)) {
    if (-not $entry) { continue }
    if (-not $entry.pid -or [string]::IsNullOrWhiteSpace([string]$entry.name) -or [string]::IsNullOrWhiteSpace([string]$entry.startTime)) {
        Write-Host "存在身份不完整的 $($entry.role) 记录，不对其执行终止。"
        $identityFailed = $true
        continue
    }

    $proc = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Host "$($entry.role)（PID $($entry.pid)）已经退出。"
        continue
    }

    $sameName = ($proc.ProcessName -eq $entry.name)
    $sameStart = $false
    try {
        $recordedStart = [datetime]$entry.startTime
        $sameStart = ([math]::Abs(($proc.StartTime - $recordedStart).TotalSeconds) -lt 1)
    } catch {
        $sameStart = $false
    }

    if (-not ($sameName -and $sameStart)) {
        Write-Host "安全保护：PID $($entry.pid) 当前身份与记录不一致，可能已被其他程序复用；不终止。"
        $identityFailed = $true
        continue
    }

    Write-Host "停止 $($entry.role) 进程树：PID $($entry.pid)，$($entry.name)"
    & taskkill /PID $entry.pid /T /F 2>$null | Out-Null
    $taskkillExit = $LASTEXITCODE

    $gone = $false
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline) {
        $still = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        if (-not $still) {
            $gone = $true
            break
        }
        if ($still.ProcessName -ne $entry.name) {
            # PID 已经被复用，原记录进程已退出；绝不处理新进程。
            $gone = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }

    if (-not $gone) {
        Write-Host "停止失败：$($entry.role)（PID $($entry.pid)）仍存在，taskkill 退出码 $taskkillExit。"
        $killFailed += $entry
    }
}

if ($identityFailed -or $killFailed.Count -gt 0) {
    Write-Host ''
    Write-Host '未能安全确认全部记录进程均已退出。状态文件保留，便于人工检查；不会扩大终止范围。'
    exit 1
}

Remove-Item -Path $stateFile -Force
Write-Host ''
Write-Host '已确认本地启动器记录的全部进程退出。'
Write-Host '状态文件已删除。没有按端口号终止任何来源不明的进程。'
