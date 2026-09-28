[CmdletBinding()]
param(
    [string]$RuntimeManifest,
    [string]$TestDataRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$testMode = $env:ANXINBOARD_DIAGNOSTICS_TEST_MODE -eq '1'

function Test-IsReparsePoint {
    param([System.IO.FileSystemInfo]$Item)
    return (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
}

function New-SafeRuntimeSummary {
    param([string]$StateFile)

    $summary = [ordered]@{
        present = (Test-Path -LiteralPath $StateFile -PathType Leaf)
        parse_status = 'not_present'
        created_at = $null
        backend_port = $null
        frontend_port = $null
        processes = @()
    }
    if (-not $summary.present) { return $summary }

    try {
        $stateItem = Get-Item -LiteralPath $StateFile -Force
        if (Test-IsReparsePoint -Item $stateItem) {
            $summary.parse_status = 'unsafe_path'
            return $summary
        }
        $state = Get-Content -LiteralPath $StateFile -Raw -Encoding utf8 | ConvertFrom-Json
        $summary.parse_status = 'ok'
        if ($state.PSObject.Properties.Name -contains 'createdAt') {
            $summary.created_at = [string]$state.createdAt
        }
        if ($state.PSObject.Properties.Name -contains 'backendPort') {
            $port = [int]$state.backendPort
            if ($port -ge 1 -and $port -le 65535) { $summary.backend_port = $port } else { $summary.parse_status = 'invalid' }
        }
        if ($state.PSObject.Properties.Name -contains 'frontendPort' -and $null -ne $state.frontendPort) {
            $port = [int]$state.frontendPort
            if ($port -ge 1 -and $port -le 65535) { $summary.frontend_port = $port } else { $summary.parse_status = 'invalid' }
        }
        if ($state.PSObject.Properties.Name -contains 'processes') {
            $safeProcesses = @()
            foreach ($process in @($state.processes)) {
                $pidValue = if ($process.PSObject.Properties.Name -contains 'pid') { [int]$process.pid } else { $null }
                $nameValue = if ($process.PSObject.Properties.Name -contains 'name') { [string]$process.name } else { $null }
                $observed = 'not_checked'
                if ($pidValue -and $pidValue -gt 0 -and -not [string]::IsNullOrWhiteSpace($nameValue)) {
                    $live = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
                    if (-not $live) { $observed = 'not_running' }
                    elseif ([string]$live.ProcessName -eq $nameValue) { $observed = 'identity_present' }
                    else { $observed = 'identity_mismatch' }
                }
                $safeProcesses += [ordered]@{
                    role = if ($process.PSObject.Properties.Name -contains 'role') { [string]$process.role } else { $null }
                    pid = $pidValue
                    name = $nameValue
                    observed = $observed
                }
            }
            $summary.processes = $safeProcesses
        }
    } catch {
        $summary.parse_status = 'invalid'
    }
    return $summary
}

function New-RuntimeManifestSummary {
    param([string]$ManifestPath)

    $summary = [ordered]@{
        present = $false
        parse_status = 'not_present'
        schema_version = $null
        source_commit = $null
        file_count = $null
    }
    if ([string]::IsNullOrWhiteSpace($ManifestPath)) { return $summary }
    if (-not [System.IO.Path]::IsPathRooted($ManifestPath)) { throw 'Runtime manifest path must be absolute.' }
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) { return $summary }

    $item = Get-Item -LiteralPath $ManifestPath -Force
    $summary.present = $true
    if (Test-IsReparsePoint -Item $item) {
        $summary.parse_status = 'unsafe_path'
        return $summary
    }

    try {
        $manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
        $summary.parse_status = 'ok'
        if ($manifest.PSObject.Properties.Name -contains 'schema_version') {
            $summary.schema_version = [string]$manifest.schema_version
        }
        if ($summary.schema_version -ne 'anxin_windows_runtime_payload_v1') { $summary.parse_status = 'invalid' }
        if ($manifest.PSObject.Properties.Name -contains 'source_commit') {
            $sourceCommit = [string]$manifest.source_commit
            if ($sourceCommit -match '^[0-9a-fA-F]{40}$' -or $sourceCommit -eq 'unknown') {
                $summary.source_commit = $sourceCommit.ToLowerInvariant()
            } else {
                $summary.parse_status = 'invalid'
                $summary.source_commit = $null
            }
        }
        if ($manifest.PSObject.Properties.Name -contains 'files') {
            $summary.file_count = @($manifest.files).Count
            if ($summary.file_count -lt 1) { $summary.parse_status = 'invalid' }
        } else {
            $summary.parse_status = 'invalid'
        }
    } catch {
        $summary.parse_status = 'invalid'
    }
    return $summary
}

function Test-LoopbackHealth {
    param([Nullable[int]]$Port)
    if ($null -eq $Port -or $Port -lt 1 -or $Port -gt 65535) {
        return [ordered]@{ status = 'not_available'; http_status = $null }
    }
    $response = $null
    try {
        $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/api/health")
        $request.Proxy = $null
        $request.Method = 'GET'
        $request.Timeout = 1500
        $request.ReadWriteTimeout = 1500
        $response = $request.GetResponse()
        $code = [int]$response.StatusCode
        if ($code -eq 200) { return [ordered]@{ status = 'reachable'; http_status = $code } }
        return [ordered]@{ status = 'unexpected_status'; http_status = $code }
    } catch {
        return [ordered]@{ status = 'unreachable'; http_status = $null }
    } finally {
        if ($response) { try { $response.Close() } catch { } }
    }
}

if (-not [string]::IsNullOrWhiteSpace($TestDataRoot)) {
    if (-not $testMode) {
        throw 'TestDataRoot is available only in explicit diagnostics test mode.'
    }
    if (-not [System.IO.Path]::IsPathRooted($TestDataRoot)) { throw 'TestDataRoot must be absolute.' }
    $dataRoot = [System.IO.Path]::GetFullPath($TestDataRoot)
} else {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw 'LOCALAPPDATA is unavailable; diagnostics will not inspect another location.'
    }
    $dataRoot = Join-Path $env:LOCALAPPDATA 'AnxinBoard'
}
if ($dataRoot -match '^(\\\\|//)') { throw 'Diagnostics data root must be local.' }

if (-not [string]::IsNullOrWhiteSpace($RuntimeManifest)) {
    if (-not $testMode) {
        throw 'RuntimeManifest override is available only in explicit diagnostics test mode.'
    }
    if (-not [System.IO.Path]::IsPathRooted($RuntimeManifest)) { throw 'RuntimeManifest must be absolute.' }
    $manifestPath = [System.IO.Path]::GetFullPath($RuntimeManifest)
} else {
    # Installed layout: <version>\AnxinBoard.Runtime\tools\collect-diagnostics.ps1
    # The only production manifest authority is <version>\runtime-manifest.json.
    $runtimeRoot = Split-Path -Parent $PSScriptRoot
    $versionRoot = Split-Path -Parent $runtimeRoot
    $manifestPath = Join-Path $versionRoot 'runtime-manifest.json'
}

if (Test-Path -LiteralPath $dataRoot) {
    $dataRootItem = Get-Item -LiteralPath $dataRoot -Force
    if (Test-IsReparsePoint -Item $dataRootItem) { throw 'Diagnostics data root is a reparse point; refusing collection.' }
    if (-not $dataRootItem.PSIsContainer) { throw 'Diagnostics data root is not a directory.' }
} else {
    New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
    $dataRootItem = Get-Item -LiteralPath $dataRoot -Force
    if (Test-IsReparsePoint -Item $dataRootItem) { throw 'Diagnostics data root became a reparse point; refusing collection.' }
}

$diagnosticsDir = Join-Path $dataRoot 'diagnostics'
if (Test-Path -LiteralPath $diagnosticsDir) {
    $diagnosticsItem = Get-Item -LiteralPath $diagnosticsDir -Force
    if (Test-IsReparsePoint -Item $diagnosticsItem) { throw 'Diagnostics output directory is a reparse point; refusing collection.' }
    if (-not $diagnosticsItem.PSIsContainer) { throw 'Diagnostics output path is not a directory.' }
} else {
    New-Item -ItemType Directory -Path $diagnosticsDir -Force | Out-Null
}

$dbPath = Join-Path $dataRoot 'anxinboard.db'
$dbSummary = [ordered]@{
    present = (Test-Path -LiteralPath $dbPath -PathType Leaf)
    size_bytes = $null
    path_status = 'not_present'
}
if ($dbSummary.present) {
    try {
        $dbItem = Get-Item -LiteralPath $dbPath -Force
        if (Test-IsReparsePoint -Item $dbItem) {
            $dbSummary.path_status = 'unsafe_path'
        } else {
            $dbSummary.path_status = 'plain_file'
            $dbSummary.size_bytes = [int64]$dbItem.Length
        }
    } catch {
        $dbSummary.path_status = 'unreadable_metadata'
        $dbSummary.size_bytes = $null
    }
}

$runtimeState = New-SafeRuntimeSummary -StateFile (Join-Path $dataRoot 'local-run.json')
$runtimePayload = New-RuntimeManifestSummary -ManifestPath $manifestPath
$loopbackHealth = Test-LoopbackHealth -Port $runtimeState.backend_port

$installStatus = if (-not $runtimePayload.present) { 'runtime_payload_missing' } elseif ($runtimePayload.parse_status -ne 'ok') { 'runtime_payload_invalid' } else { 'ok' }
$runtimeStatus = if (-not $runtimeState.present) {
    'not_recorded'
} elseif ($runtimeState.parse_status -eq 'unsafe_path') {
    'state_unsafe_path'
} elseif ($runtimeState.parse_status -ne 'ok') {
    'state_invalid'
} elseif (@($runtimeState.processes | Where-Object { $_.observed -eq 'identity_mismatch' }).Count -gt 0) {
    'process_identity_mismatch'
} elseif (@($runtimeState.processes | Where-Object { $_.observed -eq 'not_running' }).Count -gt 0) {
    'recorded_process_not_running'
} else {
    'recorded'
}
$databaseStatus = if (-not $dbSummary.present) {
    'database_missing'
} elseif ($dbSummary.path_status -eq 'unsafe_path') {
    'database_unsafe_path'
} elseif ($null -eq $dbSummary.size_bytes -or $dbSummary.size_bytes -le 0) {
    'database_empty_or_unreadable_metadata'
} else {
    'present'
}

$failureDomains = [ordered]@{
    install = [ordered]@{ status = $installStatus; destructive_repair = 'not_performed' }
    runtime = [ordered]@{ status = $runtimeStatus; destructive_repair = 'not_performed' }
    port = [ordered]@{ status = [string]$loopbackHealth.status; endpoint = '127.0.0.1/api/health'; external_network = 'not_used' }
    session = [ordered]@{ status = 'not_inspected'; reason = 'browser_session_is_memory_only'; session_token = 'never_collected' }
    database = [ordered]@{ status = $databaseStatus; content_inspection = 'not_performed' }
    config = [ordered]@{ status = 'not_inspected'; reason = 'credential_values_are_outside_diagnostics'; secret_values = 'never_collected' }
}

$summary = [ordered]@{
    schema_version = 'anxin_windows_diagnostics_v1'
    collected_at = [DateTimeOffset]::UtcNow.ToString('o')
    database = $dbSummary
    runtime_state = $runtimeState
    runtime_payload = $runtimePayload
    loopback_health = $loopbackHealth
    failure_domains = $failureDomains
    credential_store = [ordered]@{ inspection = 'not_performed'; secret_values = 'never_collected' }
    environment = [ordered]@{ enumeration = 'not_performed' }
    raw_logs = [ordered]@{ collection = 'not_performed' }
    automatic_repair = [ordered]@{ performed = $false }
}

$json = $summary | ConvertTo-Json -Depth 8 -Compress
$stamp = [DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssZ')
$suffix = [Guid]::NewGuid().ToString('N').Substring(0, 8)
$packagePath = Join-Path $diagnosticsDir "anxin-diagnostics-$stamp-$suffix.zip"

Add-Type -AssemblyName System.IO.Compression
$stream = $null
$archive = $null
$writer = $null
try {
    $stream = [System.IO.File]::Open($packagePath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $archive = New-Object System.IO.Compression.ZipArchive($stream, [System.IO.Compression.ZipArchiveMode]::Create, $false)
    $entry = $archive.CreateEntry('summary.json', [System.IO.Compression.CompressionLevel]::Optimal)
    $writer = New-Object System.IO.StreamWriter($entry.Open(), (New-Object System.Text.UTF8Encoding($false)))
    $writer.Write($json)
    $writer.Flush()
} catch {
    if (Test-Path -LiteralPath $packagePath) { [System.IO.File]::Delete($packagePath) }
    throw
} finally {
    if ($writer) { $writer.Dispose() }
    if ($archive) { $archive.Dispose() }
    if ($stream) { $stream.Dispose() }
}

Write-Host 'ANXIN_DIAGNOSTICS=SUCCESS'
Write-Host "INSTALL_STATUS=$installStatus"
Write-Host "RUNTIME_STATUS=$runtimeStatus"
Write-Host "PORT_STATUS=$($loopbackHealth.status)"
Write-Host "DATABASE_STATUS=$databaseStatus"
Write-Host 'SESSION_STATUS=NOT_INSPECTED'
Write-Host 'CONFIG_STATUS=NOT_INSPECTED'
Write-Host "DIAGNOSTICS_PACKAGE=$packagePath"
