# 停止 AnxinBoard 开发环境：只终止 dev-processes.json 中记录、且身份核对一致的本项目进程。
# 不会仅凭端口号终止任何进程；端口被其他程序占用时只报告，不终止。
# taskkill 后会再次查询 PID 确认进程真实退出；只有全部记录进程都已退出才删除状态文件，
# 否则保留状态文件、列出失败进程并以非 0 状态退出。
# 用法：pwsh -File scripts\stop-dev.ps1

$repoRoot = Split-Path -Parent $PSScriptRoot
if ($env:ANXINBOARD_STATE_PATH) {
    $stateFile = $env:ANXINBOARD_STATE_PATH
} else {
    $stateFile = Join-Path (Join-Path $env:LOCALAPPDATA 'AnxinBoard') 'dev-processes.json'
}
$ports = 8000, 5173

function Show-PortOwners {
    foreach ($port in $ports) {
        $owners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($ownerId in $owners) {
            $ownerProc = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
            Write-Host "端口 $port 当前被占用：PID $ownerId，进程 $($ownerProc.ProcessName)。本脚本不会终止该进程。"
        }
    }
}

if (-not (Test-Path $stateFile)) {
    Write-Host "未找到状态文件 $stateFile，无法确认哪些进程属于本项目，不终止任何进程。"
    Show-PortOwners
    exit 1
}

$state = Get-Content -Path $stateFile -Raw | ConvertFrom-Json
if ($state.repoRoot -ne $repoRoot) {
    Write-Host "状态文件记录的仓库路径（$($state.repoRoot)）与当前仓库（$repoRoot）不一致，不终止任何进程。"
    exit 1
}

$identityFailed = $false
$killFailed = @()
foreach ($entry in $state.processes) {
    $proc = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Host "$($entry.role)（PID $($entry.pid)）已退出，无需终止。"
        continue
    }
    $sameName = ($proc.ProcessName -eq $entry.name)
    # 注意：ConvertFrom-Json 会把 ISO 时间字符串转成 DateTime，因此按时间值比较（容差 1 秒）
    $sameStart = $false
    try {
        $recordedStart = [datetime]$entry.startTime
        $sameStart = ([math]::Abs(($proc.StartTime - $recordedStart).TotalSeconds) -lt 1)
    } catch {
        $sameStart = $false
    }
    if (-not ($sameName -and $sameStart)) {
        Write-Host "PID $($entry.pid) 的进程身份与记录不一致（当前：$($proc.ProcessName)，记录：$($entry.name)），可能已被其他程序复用，不终止。"
        $identityFailed = $true
        continue
    }
    Write-Host "停止 $($entry.role) 进程树（PID $($entry.pid)，$($entry.name)，启动于 $($entry.startTime)）..."
    & taskkill /PID $entry.pid /T /F 2>$null | Out-Null
    $killExit = $LASTEXITCODE
    # 不信任 taskkill 退出码本身：再次查询该 PID，确认进程真实消失（最多等 5 秒）
    $gone = $false
    $deadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $deadline) {
        $still = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        if (-not $still) { $gone = $true; break }
        if ($still.ProcessName -ne $entry.name) { $gone = $true; break }  # PID 已被其他程序复用，原进程已退出
        Start-Sleep -Milliseconds 250
    }
    if (-not $gone) {
        Write-Host "停止失败：$($entry.role)（PID $($entry.pid)，$($entry.name)）在 taskkill 后仍然存在（taskkill 退出码 $killExit）。"
        $killFailed += @($entry)
    }
}

if ($identityFailed -or $killFailed.Count -gt 0) {
    if ($killFailed.Count -gt 0) {
        Write-Host '以下记录的进程未能确认结束：'
        foreach ($f in $killFailed) {
            Write-Host "  - $($f.role)：PID $($f.pid)（$($f.name)）"
        }
    }
    if ($identityFailed) {
        Write-Host '存在身份无法确认的记录，不终止对应进程。'
    }
    Write-Host '未能确认全部记录进程退出，状态文件保留待人工检查，不删除。'
    exit 1
}

# 只有全部记录进程都已确认退出，才删除状态文件
Remove-Item -Path $stateFile -Force
Write-Host '已确认全部记录进程退出，本项目进程已全部停止，状态文件已删除。'

Start-Sleep -Seconds 1
$remaining = Get-NetTCPConnection -LocalPort $ports -State Listen -ErrorAction SilentlyContinue
if ($remaining) {
    Write-Host '注意：以下端口仍被其他程序占用（与本项目无关，不终止）：'
    Show-PortOwners
} else {
    Write-Host '端口 8000 和 5173 均已释放。'
}
