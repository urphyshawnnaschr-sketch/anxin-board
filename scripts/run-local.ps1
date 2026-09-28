# AnxinBoard Windows 一键本地启动器。
# 用法（仓库根目录）：
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-local.ps1
# 停止：
#   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-local.ps1
#
# 只绑定 127.0.0.1；不会修改系统策略、服务、Runner、凭据或 provider 配置。
# 首次运行会在仓库内创建 .venv 并安装 backend/frontend 项目依赖；不会安装机器级 Python/Node。
# 端口会优先使用 8000/5173；被占用时自动选择空闲 loopback 端口。
# 本脚本启动的进程身份记录在 %LOCALAPPDATA%\AnxinBoard\local-run.json，
# stop-local.ps1 只会在 PID、进程名、启动时间和仓库路径全部核对一致后终止进程。

param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot 'apps\backend'
$frontendDir = Join-Path $repoRoot 'apps\frontend'
$venvDir = Join-Path $repoRoot '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$backendRequirements = Join-Path $backendDir 'requirements.txt'
$frontendNodeModules = Join-Path $frontendDir 'node_modules'
$stateDir = Join-Path $env:LOCALAPPDATA 'AnxinBoard'
$stateFile = Join-Path $stateDir 'local-run.json'
$processRecords = @()
$stateWritten = $false

function Fail-WithGuidance {
    param([string]$Message)
    Write-Host ''
    Write-Host "启动失败：$Message"
    exit 1
}

function Convert-ToVersion {
    param([string]$Text, [string]$Name)
    $clean = ($Text -replace '^[vV]', '').Trim()
    try {
        return [version]$clean
    } catch {
        throw "$Name 版本无法识别：$Text"
    }
}

function Test-LoopbackPortAvailable {
    param([int]$Port)
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) {
            try { $listener.Stop() } catch { }
        }
    }
}

function Get-AvailableLoopbackPort {
    param([int]$PreferredPort, [int[]]$ExcludedPorts = @())

    if (($ExcludedPorts -notcontains $PreferredPort) -and (Test-LoopbackPortAvailable -Port $PreferredPort)) {
        return $PreferredPort
    }

    for ($attempt = 0; $attempt -lt 25; $attempt++) {
        $listener = $null
        try {
            $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
            $listener.Start()
            $candidate = [int]$listener.LocalEndpoint.Port
        } finally {
            if ($listener) {
                try { $listener.Stop() } catch { }
            }
        }
        if (($ExcludedPorts -notcontains $candidate) -and (Test-LoopbackPortAvailable -Port $candidate)) {
            return $candidate
        }
    }

    throw '无法找到可用的本地端口，请关闭占用大量 localhost 端口的程序后重试。'
}

function Get-ProcessRecord {
    param($Process, [string]$Role)
    try {
        $procId = [int]$Process.Id
        $name = [string]$Process.ProcessName
        $startTime = $Process.StartTime.ToString('o')
    } catch {
        throw "无法获取 $Role 进程身份：$($_.Exception.Message)"
    }
    if (-not $procId -or [string]::IsNullOrWhiteSpace($name) -or [string]::IsNullOrWhiteSpace($startTime)) {
        throw "$Role 进程身份不完整，无法安全记录和停止。"
    }
    return [ordered]@{
        role      = $Role
        pid       = $procId
        name      = $name
        startTime = $startTime
    }
}

function Write-LocalState {
    param([int]$BackendPort, [Nullable[int]]$FrontendPort)
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
    $state = [ordered]@{
        repoRoot     = $repoRoot
        createdAt    = (Get-Date).ToString('o')
        backendPort  = $BackendPort
        frontendPort = $FrontendPort
        processes    = @($processRecords)
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -Path $stateFile -Encoding utf8
    $script:stateWritten = $true
}

function Stop-OwnedRecord {
    param($Record)
    if (-not $Record) { return }

    $proc = Get-Process -Id $Record.pid -ErrorAction SilentlyContinue
    if (-not $proc) { return }

    $sameName = ($proc.ProcessName -eq $Record.name)
    $sameStart = $false
    try {
        $recordedStart = [datetime]$Record.startTime
        $sameStart = ([math]::Abs(($proc.StartTime - $recordedStart).TotalSeconds) -lt 1)
    } catch {
        $sameStart = $false
    }

    if (-not ($sameName -and $sameStart)) {
        Write-Host "安全保护：PID $($Record.pid) 已不是本次记录的 $($Record.role) 进程，不终止。"
        return
    }

    & taskkill /PID $Record.pid /T /F 2>$null | Out-Null
}

function Wait-HttpReady {
    param([string]$Url, [int]$TimeoutSec)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $response = $null
        try {
            $request = [System.Net.HttpWebRequest]::Create($Url)
            $request.Proxy = $null
            $request.Method = 'GET'
            $request.Timeout = 2000
            $request.ReadWriteTimeout = 2000
            $response = $request.GetResponse()
            if ([int]$response.StatusCode -eq 200) { return $true }
        } catch {
            Start-Sleep -Milliseconds 400
        } finally {
            if ($response) {
                try { $response.Close() } catch { }
            }
        }
    }
    return $false
}

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    Fail-WithGuidance 'Windows LOCALAPPDATA 不可用；本启动器不会把运行状态写进仓库。'
}

if (Test-Path $stateFile) {
    Fail-WithGuidance "检测到已有本地启动记录：$stateFile。请先执行 scripts\stop-local.ps1；它只会核对并停止已记录的本项目进程。"
}

# ---- 机器级运行时只验证；项目依赖首次运行时自动准备在仓库内。 ----
$systemPython = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $systemPython) {
    $systemPython = Get-Command py.exe -ErrorAction SilentlyContinue
}

if (-not (Test-Path $venvPython -PathType Leaf)) {
    if (-not $systemPython) {
        Fail-WithGuidance '未找到 Python。请先安装 Python 3.11+；启动器不会安装机器级 Python。'
    }

    Write-Host '[准备] 首次运行：创建仓库内 Python .venv ...'
    try {
        if ($systemPython.Name -ieq 'py.exe') {
            & $systemPython.Source -3 -m venv $venvDir
        } else {
            & $systemPython.Source -m venv $venvDir
        }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython -PathType Leaf)) {
            throw "venv 创建失败，退出码 $LASTEXITCODE"
        }
    } catch {
        Fail-WithGuidance "无法创建项目 .venv：$($_.Exception.Message)"
    }
}

try {
    $pythonVersionText = (& $venvPython -c "import platform; print(platform.python_version())").Trim()
    $pythonVersion = Convert-ToVersion -Text $pythonVersionText -Name 'Python'
} catch {
    Fail-WithGuidance "项目 Python 无法运行：$($_.Exception.Message)"
}
if ($pythonVersion -lt [version]'3.11') {
    Fail-WithGuidance "需要 Python 3.11+，当前项目 .venv 为 $pythonVersionText。请删除 .venv 后用 Python 3.11+ 重新运行本启动器。"
}

$backendDepsReady = $false
try {
    & $venvPython -c "import fastapi, uvicorn, pypdf, multipart" 2>$null
    if ($LASTEXITCODE -eq 0) { $backendDepsReady = $true }
} catch {
    $backendDepsReady = $false
}
if (-not $backendDepsReady) {
    Write-Host '[准备] 安装/修复 backend 项目依赖 ...'
    & $venvPython -m pip install --disable-pip-version-check -r $backendRequirements
    if ($LASTEXITCODE -ne 0) {
        Fail-WithGuidance "backend 依赖安装失败，pip 退出码 $LASTEXITCODE。请检查网络后重新运行。"
    }
}

$nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $nodeCommand) {
    Fail-WithGuidance '未找到 Node.js。请先安装 Node.js 20+（包含 npm）；启动器不会安装机器级 Node.js。'
}
try {
    $nodeVersionText = (& $nodeCommand.Source --version).Trim()
    $nodeVersion = Convert-ToVersion -Text $nodeVersionText -Name 'Node.js'
} catch {
    Fail-WithGuidance "Node.js 无法运行：$($_.Exception.Message)"
}
if ($nodeVersion -lt [version]'20.0') {
    Fail-WithGuidance "需要 Node.js 20+，当前为 $nodeVersionText。请升级 Node.js 后重试。"
}

$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCommand) {
    $npmCommand = Get-Command npm -ErrorAction SilentlyContinue
}
if (-not $npmCommand) {
    Fail-WithGuidance 'Node.js 已找到，但 npm 不可用。请修复 Node.js/npm 安装后重试。'
}
try {
    $npmVersionText = (& $npmCommand.Source --version).Trim()
    if ([string]::IsNullOrWhiteSpace($npmVersionText)) { throw 'npm 未返回版本号' }
} catch {
    Fail-WithGuidance "npm 无法运行：$($_.Exception.Message)"
}

if (-not (Test-Path $frontendNodeModules -PathType Container)) {
    Write-Host '[准备] 首次运行：安装 frontend 项目依赖 ...'
    & $npmCommand.Source 'ci' '--prefix' $frontendDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $frontendNodeModules -PathType Container)) {
        Fail-WithGuidance "frontend 依赖安装失败，npm 退出码 $LASTEXITCODE。请检查网络后重新运行。"
    }
}

$backend = $null
$frontend = $null
$backendRecord = $null
$frontendRecord = $null

try {
    $backendPort = Get-AvailableLoopbackPort -PreferredPort 8000
    $backendUrl = "http://127.0.0.1:$backendPort"
    Write-Host "[1/3] 启动后端：$backendUrl"

    $backend = Start-Process -FilePath $venvPython `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$backendPort") `
        -WorkingDirectory $backendDir -PassThru -NoNewWindow
    $backendRecord = Get-ProcessRecord -Process $backend -Role 'backend'
    $processRecords += $backendRecord
    Write-LocalState -BackendPort $backendPort -FrontendPort $null

    $backendHealth = "$backendUrl/api/health"
    if (-not (Wait-HttpReady -Url $backendHealth -TimeoutSec 30)) {
        throw "后端未在 30 秒内通过健康检查：$backendHealth"
    }
    Write-Host "      后端就绪：$backendHealth"

    $frontendPort = Get-AvailableLoopbackPort -PreferredPort 5173 -ExcludedPorts @($backendPort)
    $frontendUrl = "http://127.0.0.1:$frontendPort"
    Write-Host "[2/3] 启动前端：$frontendUrl"

    $oldBackendPort = [Environment]::GetEnvironmentVariable('ANXINBOARD_DEV_BACKEND_PORT', 'Process')
    $oldFrontendPort = [Environment]::GetEnvironmentVariable('ANXINBOARD_DEV_FRONTEND_PORT', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('ANXINBOARD_DEV_BACKEND_PORT', "$backendPort", 'Process')
        [Environment]::SetEnvironmentVariable('ANXINBOARD_DEV_FRONTEND_PORT', "$frontendPort", 'Process')
        $frontend = Start-Process -FilePath 'cmd.exe' `
            -ArgumentList @('/d', '/s', '/c', 'npm run dev') `
            -WorkingDirectory $frontendDir -PassThru -NoNewWindow
    } finally {
        [Environment]::SetEnvironmentVariable('ANXINBOARD_DEV_BACKEND_PORT', $oldBackendPort, 'Process')
        [Environment]::SetEnvironmentVariable('ANXINBOARD_DEV_FRONTEND_PORT', $oldFrontendPort, 'Process')
    }

    $frontendRecord = Get-ProcessRecord -Process $frontend -Role 'frontend'
    $processRecords += $frontendRecord
    Write-LocalState -BackendPort $backendPort -FrontendPort $frontendPort

    if (-not (Wait-HttpReady -Url $frontendUrl -TimeoutSec 30)) {
        throw "前端未在 30 秒内可访问：$frontendUrl"
    }
    Write-Host "      前端就绪：$frontendUrl"

    if ($NoBrowser) {
        Write-Host '[3/3] 已按 NoBrowser 跳过自动打开浏览器。'
    } else {
        Write-Host '[3/3] 打开默认浏览器...'
        try {
            Start-Process $frontendUrl | Out-Null
            Write-Host '      已请求 Windows 打开默认浏览器。'
        } catch {
            Write-Host "      浏览器未能自动打开，但软件已经启动。请手动打开：$frontendUrl"
        }
    }
} catch {
    Write-Host ''
    Write-Host "启动失败：$($_.Exception.Message)"
    Write-Host '正在清理本次启动且身份仍匹配的进程；不会终止启动前存在的其他程序。'

    foreach ($record in @($frontendRecord, $backendRecord)) {
        Stop-OwnedRecord -Record $record
    }
    if ($stateWritten -and (Test-Path $stateFile)) {
        Remove-Item -Path $stateFile -Force -ErrorAction SilentlyContinue
    }
    exit 1
}

Write-Host ''
Write-Host '========================================'
Write-Host 'AnxinBoard 本地软件已启动成功'
Write-Host "页面：$frontendUrl"
Write-Host "后端：$backendHealth"
Write-Host "端口：frontend=$frontendPort / backend=$backendPort（仅 127.0.0.1）"
Write-Host "停止：powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop-local.ps1"
Write-Host '========================================'
