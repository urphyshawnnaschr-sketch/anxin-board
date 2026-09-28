# 启动 AnxinBoard 开发环境：后端 FastAPI + 前端 Vite
# 用法：pwsh -File scripts\start-dev.ps1
# 停止：pwsh -File scripts\stop-dev.ps1
# 启动成功后会把本项目启动的进程身份（PID、进程名、启动时间、仓库路径）
# 记录到 %LOCALAPPDATA%\AnxinBoard\dev-processes.json，供 stop 脚本核对后终止。
# 状态文件路径可通过 ANXINBOARD_STATE_PATH 环境变量覆盖（仅测试使用）；未设置时行为保持不变。
# 从启动第一个进程开始的全部步骤（启动后端、启动前端、记录身份、写状态文件、健康检查）
# 都在统一异常处理内：任何一步失败，只清理本脚本本次启动的进程和本次生成的
# 状态文件，确认端口没有留下本次启动的监听，然后以非 0 状态退出；
# 不终止启动前已经存在的其他进程。

$ErrorActionPreference = 'Stop'
$scriptStart = Get-Date
$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot 'apps\backend'
$frontendDir = Join-Path $repoRoot 'apps\frontend'
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if ($env:ANXINBOARD_STATE_PATH) {
    $stateFile = $env:ANXINBOARD_STATE_PATH
    $stateDir = Split-Path -Parent $stateFile
} else {
    $stateDir = Join-Path $env:LOCALAPPDATA 'AnxinBoard'
    $stateFile = Join-Path $stateDir 'dev-processes.json'
}

if (-not (Test-Path $venvPython)) {
    Write-Host '未找到 .venv，请先执行：python -m venv .venv; .venv\Scripts\pip install -r apps\backend\requirements.txt'
    exit 1
}
if (-not (Test-Path (Join-Path $frontendDir 'node_modules'))) {
    Write-Host '未找到 node_modules，请先执行：cd apps\frontend; npm install'
    exit 1
}

# 端口占用检查：8000 或 5173 被占用时直接失败，只报告占用者信息，不终止任何进程
$occupied = $false
foreach ($port in 8000, 5173) {
    $owners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($ownerId in $owners) {
        $ownerProc = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
        Write-Host "端口 $port 已被占用：PID $ownerId，进程 $($ownerProc.ProcessName)。本脚本不会终止该进程。"
        $occupied = $true
    }
}
if ($occupied) {
    Write-Host '如占用者是本项目，请执行 pwsh -File scripts\stop-dev.ps1；否则请人工处理后重试。'
    exit 1
}

# 获取进程身份；PID、进程名或启动时间任一无法取得即抛错，判定启动失败，
# 不允许生成 stop 脚本无法核验的不完整状态记录
function Get-ProcessRecord {
    param($Process, [string]$Role)
    try {
        $procId = $Process.Id
        $name = $Process.ProcessName
        $startTime = $Process.StartTime.ToString('o')
    } catch {
        throw "无法获取 $Role 进程的完整身份：$($_.Exception.Message)"
    }
    if (-not $procId -or [string]::IsNullOrWhiteSpace($name) -or [string]::IsNullOrWhiteSpace($startTime)) {
        throw "$Role 进程身份不完整（PID=$procId，进程名=$name，启动时间=$startTime），判定启动失败"
    }
    return [ordered]@{
        role      = $Role
        pid       = $procId
        name      = $name
        startTime = $startTime
    }
}

function Wait-HttpReady {
    param([string]$Url, [int]$TimeoutSec)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -NoProxy -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return $true }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

# —— 从启动第一个进程开始，全部步骤纳入统一异常处理 ——
$backend = $null
$frontend = $null
$stateFileWritten = $false

try {
    Write-Host '启动后端 (http://127.0.0.1:8000) ...'
    $backend = Start-Process -FilePath $venvPython `
        -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000' `
        -WorkingDirectory $backendDir -PassThru

    Write-Host '启动前端 (http://127.0.0.1:5173) ...'
    $frontend = Start-Process -FilePath 'cmd.exe' `
        -ArgumentList '/c', 'npm run dev' `
        -WorkingDirectory $frontendDir -PassThru

    # 记录进程身份；任一身份不完整会抛错并进入统一清理
    $records = @(
        (Get-ProcessRecord -Process $backend -Role 'backend'),
        (Get-ProcessRecord -Process $frontend -Role 'frontend')
    )

    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    $state = [ordered]@{
        repoRoot  = $repoRoot
        createdAt = (Get-Date).ToString('o')
        processes = $records
    }
    $state | ConvertTo-Json -Depth 4 | Set-Content -Path $stateFile -Encoding utf8
    $stateFileWritten = $true

    # 启动后确认两个地址真实可访问
    $backendOk = Wait-HttpReady -Url 'http://127.0.0.1:8000/api/health' -TimeoutSec 30
    $frontendOk = Wait-HttpReady -Url 'http://127.0.0.1:5173' -TimeoutSec 30
    if (-not ($backendOk -and $frontendOk)) {
        throw "健康检查失败：后端可访问=$backendOk，前端可访问=$frontendOk"
    }
} catch {
    Write-Host "启动失败：$($_.Exception.Message)"
    Write-Host '正在清理本脚本本次启动的进程和本次生成的状态文件（不影响启动前已存在的其他进程）...'
    foreach ($proc in @($backend, $frontend)) {
        if (-not $proc) { continue }
        $exited = $false
        try { $exited = $proc.HasExited } catch { $exited = $false }
        if (-not $exited) {
            & taskkill /PID $proc.Id /T /F 2>$null | Out-Null
        }
    }
    if ($stateFileWritten) {
        Remove-Item -Path $stateFile -Force -ErrorAction SilentlyContinue
    }
    # 确认 8000 和 5173 没有留下本次启动的监听；启动前已存在的进程一律不终止
    Start-Sleep -Seconds 1
    $ownPids = @($backend, $frontend) | Where-Object { $_ } | ForEach-Object { $_.Id }
    $leftover = $false
    foreach ($port in 8000, 5173) {
        $owners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($ownerId in $owners) {
            $ownerProc = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
            if ($ownPids -contains $ownerId) {
                Write-Host "警告：端口 $port 仍被本次启动的进程（PID $ownerId）占用，请人工检查。"
                $leftover = $true
            } elseif ($ownerProc -and $ownerProc.StartTime -ge $scriptStart) {
                Write-Host "警告：端口 $port 被 PID $ownerId（$($ownerProc.ProcessName)，启动于本脚本运行之后）占用，可能是本次启动的子进程残留，请人工检查。"
                $leftover = $true
            } else {
                Write-Host "端口 $port 被启动前已存在的进程（PID $ownerId，$($ownerProc.ProcessName)）占用，与本次启动无关，不终止。"
            }
        }
    }
    if (-not $leftover) {
        Write-Host '已确认端口 8000 和 5173 没有留下本次启动的监听。'
    }
    exit 1
}

Write-Host ''
Write-Host '后端已就绪： http://127.0.0.1:8000/api/health'
Write-Host '页面地址：   http://127.0.0.1:5173'
Write-Host "进程记录：   $stateFile"
Write-Host '停止方式：   pwsh -File scripts\stop-dev.ps1（仅终止上述记录且身份核对一致的进程）'
