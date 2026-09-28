# 开发环境生命周期一键自动验证脚本
# 用法：pwsh -NoProfile -File scripts\test-dev-lifecycle.ps1
#
# 覆盖场景（每个场景输出 PASS / FAIL）：
#   1. 正常启动       —— start-dev.ps1 正常拉起后端+前端，状态文件与临时数据库生成，健康检查通过，
#                       并递归捕获本次前后端进程树（Python / cmd / Node / Vite 子孙进程）身份
#   2. 重复启动保护   —— 环境已启动时再次执行 start-dev.ps1 必须失败，且不影响已在运行的进程与状态文件
#   3. 异常启动清理   —— 状态文件写入失败（只读文件）时，start-dev.ps1 必须清理本次启动的进程并释放端口
#   4. 正常停止       —— stop-dev.ps1 终止记录中的进程、确认退出、删除状态文件并释放端口；
#                       同时核验本次捕获的整个进程树（含 cmd / Node / Vite 子孙）已全部消失
#   5. 防止误杀       —— 无关进程（python -m http.server 5173）占用端口时，stop-dev.ps1 不得终止它
#   6. 残留检查       —— 全部结束后端口已释放、本次捕获的全部进程身份已消失、
#                       正式数据库/正式状态文件未被读写、临时目录已清理
#
# 进程身份核验：脚本为每次启动（后端/前端根进程）及启动的无关进程递归记录 PID、进程名、
# 启动时间、父进程关系与测试角色；停止与残留检查按 进程名+启动时间 核验（PID 可能被系统复用），
# 最终清理仅在 PID、进程名、启动时间三者一致时才允许 taskkill，身份不一致时不得终止并记为 FAIL。
#
# 测试隔离：临时目录 %TEMP%\AnxinBoard-lifecycle-<随机>\ 下的临时 SQLite 数据库与临时状态文件，
# 通过 ANXINBOARD_DB_PATH / ANXINBOARD_STATE_PATH 注入启停脚本，结束时统一清理。
# 不读取、不写入正式数据库（%LOCALAPPDATA%\AnxinBoard\anxinboard.db）与正式状态文件
# （%LOCALAPPDATA%\AnxinBoard\dev-processes.json），全程只读校验它们未被改动。
#
# 退出码：6 个场景全部 PASS 退出 0；任一场景 FAIL 退出非 0。
# 遇到未预期异常时仍执行统一清理，追加一条“测试执行异常：FAIL”，最终输出总体 FAIL 并返回非 0。
# 说明：本脚本不覆盖 OS 级 PID 真实复用（只能通过身份不一致检测并拒绝误杀，不模拟复用）、
# 受限沙箱下 taskkill 拒绝访问、以及 Windows 权限异常，这些情况无法在普通开发机上稳定复现，
# 属于已知不自动化项。

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$startScript = Join-Path $PSScriptRoot 'start-dev.ps1'
$stopScript = Join-Path $PSScriptRoot 'stop-dev.ps1'
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$backendHealth = 'http://127.0.0.1:8000/api/health'
$frontendUrl = 'http://127.0.0.1:5173'
$ports = 8000, 5173

$script:results = [System.Collections.Generic.List[object]]::new()
$script:capturedIdentities = [System.Collections.Generic.List[object]]::new()
$script:topLevelError = $null
$script:overallExit = 1

function Add-Result {
    param([string]$Name, [bool]$Pass, [string]$Detail)
    $script:results.Add([pscustomobject]@{ Name = $Name; Pass = $Pass; Detail = $Detail })
}

function Test-PortListening {
    param([int]$Port)
    $owners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    return (@($owners).Count -gt 0)
}

function Assert-PortsFree {
    foreach ($p in $ports) {
        if (Test-PortListening $p) {
            $owners = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique
            foreach ($o in $owners) {
                $pr = Get-Process -Id $o -ErrorAction SilentlyContinue
                Write-Host "  [预检] 端口 $p 仍被 PID $o（$($pr.ProcessName)）占用，测试可能受影响。"
            }
            return $false
        }
    }
    return $true
}

function Invoke-ChildScript {
    param([string]$ScriptPath)
    $out = & pwsh -NoProfile -NonInteractive -File $ScriptPath 2>&1 | Out-String
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $out }
}

function Http-Ready {
    param([string]$Url, [int]$TimeoutSec = 30)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -NoProxy -TimeoutSec 3
            if ($resp.StatusCode -eq 200) { return $true }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    return $false
}

function Test-ProcessAlive {
    param([int]$ProcessId)
    return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

# 递归捕获从根 PID 出发的整个进程树（含子孙进程），返回 PID/进程名/启动时间/父PID 列表
function Get-ProcessTreeIdentities {
    param([int]$RootPid)
    $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $index = @{}
    foreach ($p in $all) {
        if ($p.ProcessId) { $index[[int]$p.ProcessId] = $p }
    }
    $out = [System.Collections.Generic.List[object]]::new()
    $visited = @{}
    $queue = [System.Collections.Generic.Queue[int]]::new()
    $queue.Enqueue($RootPid)
    while ($queue.Count -gt 0) {
        $cur = $queue.Dequeue()
        if ($visited.ContainsKey($cur)) { continue }
        $visited[$cur] = $true
        $proc = $index[$cur]
        if ($proc) {
            $name = ''
            try { $name = [System.IO.Path]::GetFileNameWithoutExtension($proc.Name) } catch { $name = '' }
            $start = ''
            try { $start = $proc.CreationDate.ToString('o') } catch { $start = '' }
            if (-not $start) {
                try { $start = (Get-Process -Id $cur -ErrorAction Stop).StartTime.ToString('o') } catch { $start = '' }
            }
            $out.Add([pscustomobject]@{ Pid = $cur; Name = $name; StartTime = $start; ParentPid = [int]$proc.ParentProcessId })
        }
        foreach ($child in $all) {
            if ($child.ParentProcessId -and ([int]$child.ParentProcessId -eq $cur)) {
                $queue.Enqueue([int]$child.ProcessId)
            }
        }
    }
    return $out
}

# 捕获本次前后端进程树并打上测试角色（backend/frontend，根进程为 -root，子孙为 -child）
function Capture-ProcessTrees {
    param([int]$BackendPid, [int]$FrontendPid)
    $list = [System.Collections.Generic.List[object]]::new()
    foreach ($pair in @(
        @{ Root = $BackendPid; Role = 'backend' },
        @{ Root = $FrontendPid; Role = 'frontend' }
    )) {
        if (-not $pair.Root) { continue }
        foreach ($node in Get-ProcessTreeIdentities -RootPid $pair.Root) {
            $role = if ($node.Pid -eq $pair.Root) { "$($pair.Role)-root" } else { "$($pair.Role)-child" }
            $list.Add([pscustomobject]@{
                Pid       = $node.Pid
                Name      = $node.Name
                StartTime = $node.StartTime
                ParentPid = $node.ParentPid
                Role      = $role
            })
        }
    }
    return $list
}

# 按 进程名+启动时间 核验 PID 对应的“同一进程”是否仍存活；PID 被复用（身份不一致）视为原进程已消失
function Test-IdentityMatches {
    param([object]$Id)
    $proc = Get-Process -Id $Id.Pid -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    if ($proc.ProcessName -ne $Id.Name) { return $false }
    $startMatch = $false
    try {
        $rec = [datetime]$Id.StartTime
        $startMatch = ([math]::Abs(($proc.StartTime - $rec).TotalSeconds) -lt 1)
    } catch { $startMatch = $false }
    return $startMatch
}

# 返回仍与记录身份一致（即仍然存活）的进程身份列表
function Get-IdentitiesStillAlive {
    param($Identities)
    $alive = [System.Collections.Generic.List[object]]::new()
    foreach ($id in $Identities) {
        if (Test-IdentityMatches $id) { $alive.Add($id) }
    }
    return $alive
}

# 校验进程树捕获结果有效：非空、存在 backend-root / frontend-root、至少一个前端 Node 子进程、
# 且每条身份的 PID / 进程名 / 启动时间均完整。任一不满足返回 $false。
function Test-CaptureValid {
    param($Identities)
    $list = @($Identities)
    if ($list.Count -eq 0) { return $false }
    $hasBackendRoot = @($list | Where-Object { $_.Role -eq 'backend-root' }).Count -gt 0
    $hasFrontendRoot = @($list | Where-Object { $_.Role -eq 'frontend-root' }).Count -gt 0
    $hasFrontendNode = @($list | Where-Object { $_.Role -eq 'frontend-child' -and $_.Name -eq 'node' }).Count -gt 0
    $complete = @($list | Where-Object {
        -not $_.Pid -or [string]::IsNullOrWhiteSpace($_.Name) -or [string]::IsNullOrWhiteSpace($_.StartTime)
    }).Count -eq 0
    return $hasBackendRoot -and $hasFrontendRoot -and $hasFrontendNode -and $complete
}

function Track-Identities {
    param($Identities)
    foreach ($id in $Identities) {
        $script:capturedIdentities.Add($id)
    }
}

function Describe-Identities {
    param($Identities, [string]$Indent = '    ')
    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($id in $Identities) {
        $lines.Add("$Indent$($id.Role.PadRight(15)) PID=$($id.Pid)  Name=$($id.Name)  Start=$($id.StartTime)")
    }
    return $lines -join [Environment]::NewLine
}

function Remove-TestStateFile {
    param([string]$Path)
    $item = Get-Item -Path $Path -ErrorAction SilentlyContinue
    if ($item) {
        Set-ItemProperty -Path $item.FullName -Name IsReadOnly -Value $false -ErrorAction SilentlyContinue
        Remove-Item -Path $item.FullName -Force -ErrorAction SilentlyContinue
    }
}

function Show-ResultSummary {
    Write-Host ''
    Write-Host '===== 开发环境生命周期测试结果 ====='
    foreach ($res in $script:results) {
        $flag = if ($res.Pass) { 'PASS' } else { 'FAIL' }
        Write-Host "$flag  $($res.Name)"
        if (-not $res.Pass) { Write-Host "        $($res.Detail)" }
    }
    Write-Host ''
}

# 统一清理（幂等）：先用 stop-dev.ps1 做身份核对清理，再对捕获身份逐一核验后 taskkill，
# 身份不一致（疑似 PID 复用）不得终止并记为 FAIL；随后删除测试临时目录并恢复环境变量。
function Invoke-UnifiedCleanup {
    if (Test-Path $testState) {
        try {
            $st = Get-Content -Path $testState -Raw | ConvertFrom-Json
            if ($st.repoRoot -eq $repoRoot -and $st.processes) {
                $null = & pwsh -NoProfile -NonInteractive -File $stopScript 2>&1 | Out-Null
            }
        } catch { }
    }
    $cleanupReports = [System.Collections.Generic.List[string]]::new()
    $cleanupMismatch = $false
    foreach ($id in @($script:capturedIdentities)) {
        $proc = Get-Process -Id $id.Pid -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        if (-not (Test-IdentityMatches $id)) {
            $cleanupMismatch = $true
            $cleanupReports.Add("PID $($id.Pid)（记录名 $($id.Name)，实际 $($proc.ProcessName)）身份不一致，疑似 PID 复用，未终止。")
            continue
        }
        & taskkill /PID $id.Pid /T /F 2>$null | Out-Null
        $gone = $false
        $deadline = (Get-Date).AddSeconds(5)
        while ((Get-Date) -lt $deadline) {
            $still = Get-Process -Id $id.Pid -ErrorAction SilentlyContinue
            if (-not $still) { $gone = $true; break }
            if ($still.ProcessName -ne $id.Name) { $gone = $true; break }
            Start-Sleep -Milliseconds 200
        }
        if (-not $gone) {
            $cleanupReports.Add("PID $($id.Pid)（$($id.Name)）清理后仍存在，未能确认结束。")
        }
    }
    if ($cleanupMismatch) {
        Add-Result '清理身份核验' $false ($cleanupReports -join '；')
    }
    Remove-TestStateFile $testState
    Remove-Item -Path $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item Env:\ANXINBOARD_DB_PATH -ErrorAction SilentlyContinue
    Remove-Item Env:\ANXINBOARD_STATE_PATH -ErrorAction SilentlyContinue
}

# 正式数据库 / 正式状态文件（只读校验用，绝不读写）
$realDbPath = Join-Path (Join-Path $env:LOCALAPPDATA 'AnxinBoard') 'anxinboard.db'
$realStatePath = Join-Path (Join-Path $env:LOCALAPPDATA 'AnxinBoard') 'dev-processes.json'
$realDbExisted = Test-Path $realDbPath
$realDbBefore = if ($realDbExisted) { (Get-Item $realDbPath).LastWriteTimeUtc } else { [datetime]::MinValue }
$realStateExisted = Test-Path $realStatePath
$realStateBefore = if ($realStateExisted) { (Get-Item $realStatePath).LastWriteTimeUtc } else { [datetime]::MinValue }

# 测试专用临时目录与临时数据库 / 状态文件
$testRoot = Join-Path $env:TEMP ('AnxinBoard-lifecycle-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
$testDb = Join-Path $testRoot 'test.db'
$testState = Join-Path $testRoot 'state.json'

# 注入测试隔离环境变量（子进程继承）
$env:ANXINBOARD_DB_PATH = $testDb
$env:ANXINBOARD_STATE_PATH = $testState

try {
    # ============ 场景 1：正常启动 ============
    Write-Host ''
    Write-Host '=== 场景 1/6：正常启动 ==='
    if (-not (Assert-PortsFree)) {
        Add-Result '正常启动' $false '启动前 8000/5173 已被占用，无法验证正常启动。'
    } else {
        Remove-TestStateFile $testState
        $r = Invoke-ChildScript $startScript
        $stateOk = Test-Path $testState
        $state = $null
        if ($stateOk) { try { $state = Get-Content -Path $testState -Raw | ConvertFrom-Json } catch { $state = $null } }
        $roles = @()
        if ($state -and $state.processes) { $roles = @($state.processes | ForEach-Object { $_.role }) }
        $idsOk = $false
        $backendPid = 0
        $frontendPid = 0
        if ($state -and $state.processes) {
            $bad = @($state.processes | Where-Object { -not $_.pid -or [string]::IsNullOrWhiteSpace($_.name) -or [string]::IsNullOrWhiteSpace($_.startTime) })
            $idsOk = ($state.processes.Count -eq 2) -and ($bad.Count -eq 0) -and ($roles -contains 'backend') -and ($roles -contains 'frontend')
            if ($idsOk) {
                $backendPid = [int](@($state.processes | Where-Object { $_.role -eq 'backend' })[0].pid)
                $frontendPid = [int](@($state.processes | Where-Object { $_.role -eq 'frontend' })[0].pid)
            }
        }
        Start-Sleep -Seconds 3
        $captured = @()
        if ($idsOk) {
            $captured = @(Capture-ProcessTrees -BackendPid $backendPid -FrontendPid $frontendPid)
            if ($captured.Count -eq 0) {
                Start-Sleep -Seconds 2
                $captured = @(Capture-ProcessTrees -BackendPid $backendPid -FrontendPid $frontendPid)
            }
            Track-Identities $captured
        }
        $captureValid = Test-CaptureValid $captured
        $dbOk = Test-Path $testDb
        $backendOk = Http-Ready $backendHealth
        $frontendOk = Http-Ready $frontendUrl
        $pass = ($r.ExitCode -eq 0) -and $stateOk -and $idsOk -and $captureValid -and $dbOk -and $backendOk -and $frontendOk
        $detail = "退出码=$($r.ExitCode) 状态文件=$stateOk 进程记录=$idsOk 捕获进程树=$($captured.Count) 捕获有效性=$captureValid（需含 backend-root/frontend-root/前端 Node 子进程且身份完整） 数据库=$dbOk 后端健康=$backendOk 前端可达=$frontendOk"
        if (-not $pass) {
            $detail += " | 输出摘要: $((($r.Output -split '\r?\n') -join '; ').Substring(0, [Math]::Min(400, ($r.Output -split '\r?\n' -join '; ').Length)))"
        }
        Add-Result '正常启动' $pass $detail
    }

    # ============ 场景 2：重复启动保护 ============
    Write-Host '=== 场景 2/6：重复启动保护 ==='
    $s1EnvAlive = $false
    if (Test-Path $testState) {
        $s1 = Get-Content -Path $testState -Raw | ConvertFrom-Json
        $s1EnvAlive = $s1.processes.Count -gt 0 -and @($s1.processes | Where-Object { Test-ProcessAlive ([int]$_.pid) }).Count -eq $s1.processes.Count
    }
    if (-not $s1EnvAlive) {
        Add-Result '重复启动保护' $false '场景 1 环境未处于运行状态，无法验证重复启动保护。'
    } else {
        $beforeCreated = (Get-Content -Path $testState -Raw | ConvertFrom-Json).createdAt
        $r = Invoke-ChildScript $startScript
        $afterState = Get-Content -Path $testState -Raw | ConvertFrom-Json
        $aliveAfter = @($afterState.processes | Where-Object { Test-ProcessAlive ([int]$_.pid) }).Count -eq $afterState.processes.Count
        $createdUnchanged = ($afterState.createdAt -eq $beforeCreated)
        $failed = $r.ExitCode -ne 0
        $pass = $failed -and $aliveAfter -and $createdUnchanged
        $detail = "再次启动退出码=$($r.ExitCode)（期望非 0）原进程仍存活=$aliveAfter 状态文件未被覆盖=$createdUnchanged"
        if (-not $pass) {
            $detail += " | 输出摘要: $((($r.Output -split '\r?\n') -join '; ').Substring(0, [Math]::Min(400, ($r.Output -split '\r?\n' -join '; ').Length)))"
        }
        Add-Result '重复启动保护' $pass $detail
    }

    # 为场景 3 释放端口：停止场景 1 启动的环境（走 stop-dev.ps1 身份核对路径）
    if (Test-Path $testState) {
        $null = Invoke-ChildScript $stopScript
    }

    # ============ 场景 3：异常启动清理（只读状态文件） ============
    Write-Host '=== 场景 3/6：异常启动清理 ==='
    if (-not (Assert-PortsFree)) {
        Add-Result '异常启动清理' $false '启动前 8000/5173 已被占用，无法验证异常清理路径。'
    } else {
        Remove-TestStateFile $testState
        Set-Content -Path $testState -Value 'lock' -Encoding utf8
        Set-ItemProperty -Path $testState -Name IsReadOnly -Value $true
        $r = Invoke-ChildScript $startScript
        $stillReadOnly = ((Get-Item $testState).IsReadOnly -eq $true)
        Start-Sleep -Seconds 1
        $portsFree = Assert-PortsFree
        $pass = ($r.ExitCode -ne 0) -and $portsFree -and $stillReadOnly
        $detail = "退出码=$($r.ExitCode)（期望非 0）端口已释放=$portsFree 只读文件未被删除=$stillReadOnly"
        if (-not $pass) {
            $detail += " | 输出摘要: $((($r.Output -split '\r?\n') -join '; ').Substring(0, [Math]::Min(400, ($r.Output -split '\r?\n' -join '; ').Length)))"
        }
        Add-Result '异常启动清理' $pass $detail
        Remove-TestStateFile $testState
    }

    # ============ 场景 4：正常停止 ============
    Write-Host '=== 场景 4/6：正常停止 ==='
    if (-not (Assert-PortsFree)) {
        Add-Result '正常停止' $false '启动前 8000/5173 已被占用，无法验证正常启动/停止。'
    } else {
        Remove-TestStateFile $testState
        $rStart = Invoke-ChildScript $startScript
        $pids = @()
        $captured4 = @()
        if (Test-Path $testState) {
            $st = Get-Content -Path $testState -Raw | ConvertFrom-Json
            $pids = @($st.processes | ForEach-Object { [int]$_.pid })
            $bkPid = [int](@($st.processes | Where-Object { $_.role -eq 'backend' })[0].pid)
            $ftPid = [int](@($st.processes | Where-Object { $_.role -eq 'frontend' })[0].pid)
            Start-Sleep -Seconds 2
            $captured4 = @(Capture-ProcessTrees -BackendPid $bkPid -FrontendPid $ftPid)
            Track-Identities $captured4
        }
        $captureValid4 = Test-CaptureValid $captured4
        $startOk = ($rStart.ExitCode -eq 0) -and $pids.Count -eq 2 -and $captureValid4
        if (-not $startOk) {
            Add-Result '正常停止' $false "前置启动失败（退出码=$($rStart.ExitCode) 记录进程数=$($pids.Count) 捕获有效性=$captureValid4）。"
        } else {
            $rStop = Invoke-ChildScript $stopScript
            Start-Sleep -Seconds 1
            $stateGone = -not (Test-Path $testState)
            $procsGone = @($pids | Where-Object { Test-ProcessAlive $_ }).Count -eq 0
            $treeGone = (Get-IdentitiesStillAlive $captured4).Count -eq 0
            $portsFree = Assert-PortsFree
            $pass = ($rStop.ExitCode -eq 0) -and $stateGone -and $procsGone -and $treeGone -and $portsFree
            $detail = "停止退出码=$($rStop.ExitCode) 状态文件已删除=$stateGone 记录进程已退出=$procsGone 进程树已全部退出=$treeGone（捕获 $($captured4.Count) 个，含 cmd/Node/Vite 子孙） 端口已释放=$portsFree"
            if (-not $pass) {
                $detail += " | 停止输出摘要: $((($rStop.Output -split '\r?\n') -join '; ').Substring(0, [Math]::Min(400, ($rStop.Output -split '\r?\n' -join '; ').Length)))"
            }
            Add-Result '正常停止' $pass $detail
        }
    }

    # ============ 场景 5：防止误杀（无关进程占用端口） ============
    Write-Host '=== 场景 5/6：防止误杀 ==='
    if (-not (Assert-PortsFree)) {
        Add-Result '防止误杀' $false '启动前 8000/5173 已被占用，无法放置无关进程。'
    } else {
        Remove-TestStateFile $testState
        $decoy = Start-Process -FilePath $venvPython `
            -ArgumentList '-m', 'http.server', '5173', '--bind', '127.0.0.1' `
            -WorkingDirectory $testRoot -PassThru
        $decoyPid = $decoy.Id
        $decoyStart = ''
        try { $decoyStart = $decoy.StartTime.ToString('o') } catch { $decoyStart = (Get-Process -Id $decoyPid).StartTime.ToString('o') }
        $decoyIdent = [pscustomobject]@{ Pid = $decoyPid; Name = 'python'; StartTime = $decoyStart; ParentPid = 0; Role = 'decoy' }
        Track-Identities @($decoyIdent)
        Start-Sleep -Milliseconds 800
        foreach ($node in @(Get-ProcessTreeIdentities -RootPid $decoyPid)) {
            if ($node.Pid -eq $decoyPid) { continue }
            $script:capturedIdentities.Add([pscustomobject]@{
                Pid = $node.Pid; Name = $node.Name; StartTime = $node.StartTime; ParentPid = $node.ParentPid; Role = 'decoy-child'
            })
        }
        $decoyReady = Http-Ready $frontendUrl
        $fakeState = [ordered]@{
            repoRoot  = $repoRoot
            createdAt = (Get-Date).ToString('o')
            processes = @(
                [ordered]@{ role = 'decoy'; pid = $decoyPid; name = 'python'; startTime = '2000-01-01T00:00:00.0000000+00:00' }
            )
        }
        $fakeState | ConvertTo-Json -Depth 4 | Set-Content -Path $testState -Encoding utf8
        $r = Invoke-ChildScript $stopScript
        $decoyAlive = Test-IdentityMatches $decoyIdent
        $decoyStillOwnsPort = Test-PortListening 5173
        $pass = ($r.ExitCode -ne 0) -and $decoyAlive -and $decoyStillOwnsPort -and $decoyReady
        $detail = "停止退出码=$($r.ExitCode)（期望非 0）无关进程仍存活=$decoyAlive 端口仍由其占用=$decoyStillOwnsPort 服务可达=$decoyReady"
        if (-not $pass) {
            $detail += " | 停止输出摘要: $((($r.Output -split '\r?\n') -join '; ').Substring(0, [Math]::Min(400, ($r.Output -split '\r?\n' -join '; ').Length)))"
        }
        Add-Result '防止误杀' $pass $detail
    }

# —— 场景 1-5 完成后先执行统一清理，再做残留检查（场景 6），保证残留检查在清理之后 ——
Invoke-UnifiedCleanup

# ============ 场景 6：残留检查 ============
Write-Host '=== 场景 6/6：残留检查 ==='
Start-Sleep -Seconds 1
$portsFree = Assert-PortsFree
$aliveAll = Get-IdentitiesStillAlive $script:capturedIdentities
$allCapturedGone = $aliveAll.Count -eq 0
$realDbAfter = Test-Path $realDbPath
$realDbTouched = ($realDbExisted -ne $realDbAfter) -or ($realDbExisted -and $realDbAfter -and ((Get-Item $realDbPath).LastWriteTimeUtc -gt $realDbBefore))
$realStateAfter = Test-Path $realStatePath
$realStateTouched = ($realStateExisted -ne $realStateAfter) -or ($realStateExisted -and $realStateAfter -and ((Get-Item $realStatePath).LastWriteTimeUtc -gt $realStateBefore))
$tempGone = -not (Test-Path $testRoot)
$pass = $portsFree -and $allCapturedGone -and (-not $realDbTouched) -and (-not $realStateTouched) -and $tempGone
$detail = "端口已释放=$portsFree 本次捕获进程身份已全部消失=$allCapturedGone（$($script:capturedIdentities.Count) 个） 临时目录已清理=$tempGone 正式数据库未被读写=$(-not $realDbTouched) 正式状态文件未被修改=$(-not $realStateTouched)"
Add-Result '残留检查' $pass $detail

# 展示捕获并核验的进程身份清单（Python / cmd / Node / Vite 子孙进程）
Write-Host ''
Write-Host "已捕获并核验的进程身份（共 $($script:capturedIdentities.Count) 个）："
Write-Host (Describe-Identities $script:capturedIdentities)
if ($aliveAll.Count -gt 0) {
    Write-Host '以下捕获进程在最终残留检查时仍存在：'
    Write-Host (Describe-Identities $aliveAll)
}

# ============ 汇总输出（位于顶层异常保护范围内） ============
$script:failedList = @($script:results | Where-Object { -not $_.Pass })
$script:allPass = $script:failedList.Count -eq 0
Show-ResultSummary
if ($script:allPass) {
    Write-Host '总体结果：PASS'
    $script:overallExit = 0
} else {
    Write-Host "总体结果：FAIL（失败场景：$(@($script:failedList | ForEach-Object { $_.Name }) -join '、')）"
    $script:overallExit = 1
}
} catch {
    # —— 顶层异常捕获：覆盖场景 1 至 6 及汇总阶段；仍执行统一清理，最终输出总体 FAIL 与非 0 退出码 ——
    $script:topLevelError = $_
} finally {
    # —— finally 兜底统一清理（幂等；场景 1-5 后已执行过一次，异常路径也保证执行）——
    Invoke-UnifiedCleanup
}

# 顶层异常发生时的兜底汇总：仍输出结果表、总体 FAIL 与非 0 退出码，不中断
if ($script:topLevelError) {
    Add-Result '测试执行异常' $false "未预期异常，已执行统一清理：$($script:topLevelError.Exception.Message)"
    $script:failedList = @($script:results | Where-Object { -not $_.Pass })
    Show-ResultSummary
    Write-Host "总体结果：FAIL（顶层异常：$($script:topLevelError.Exception.Message)）"
    $script:overallExit = 1
}

exit $script:overallExit
