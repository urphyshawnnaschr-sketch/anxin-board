[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$runtimeDir = Join-Path $PayloadRoot 'AnxinBoard.Runtime'
$runtimeExe = Join-Path $runtimeDir 'AnxinBoard.Runtime.exe'
$manifestPath = Join-Path $PayloadRoot 'runtime-manifest.json'
if (-not (Test-Path $runtimeExe -PathType Leaf)) { throw 'runtime executable is missing' }
if (-not (Test-Path $manifestPath -PathType Leaf)) { throw 'runtime manifest is missing' }

# Parse the user-facing restore control script with the exact Windows PowerShell parser
# before the runtime smoke can count as restore evidence. This catches syntax failures
# that Inno packaging alone would otherwise carry into a physical client.
$restoreScriptPath = Join-Path $PSScriptRoot 'restore-product-backup.ps1'
if (-not (Test-Path -LiteralPath $restoreScriptPath -PathType Leaf)) { throw 'restore control script is missing' }
$parseTokens = $null
$parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile(
    $restoreScriptPath,
    [ref]$parseTokens,
    [ref]$parseErrors
)
if (@($parseErrors).Count -ne 0) {
    @($parseErrors) | ForEach-Object { Write-Host "RESTORE_SCRIPT_PARSE_ERROR=$($_.Message)" }
    throw 'restore control script failed Windows PowerShell syntax parsing'
}
Write-Host 'RESTORE_CONTROL_SCRIPT_PARSE=SUCCESS'

$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($manifest.schema_version -ne 'anxin_windows_runtime_payload_v1') { throw 'runtime manifest schema mismatch' }

foreach ($entry in @($manifest.files)) {
    $candidate = Join-Path $runtimeDir ([string]$entry.path).Replace('/', '\')
    if (-not (Test-Path $candidate -PathType Leaf)) { throw "manifest file missing: $($entry.path)" }
    $actual = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne [string]$entry.sha256) { throw "manifest digest mismatch: $($entry.path)" }
}

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$port = [int]$listener.LocalEndpoint.Port
$listener.Stop()
$origin = "http://127.0.0.1:$port"
$bootstrap = 'lane-c-ci-bootstrap-not-a-real-secret-2026'
$oldBootstrap = [Environment]::GetEnvironmentVariable('ANXINBOARD_LOCAL_BOOTSTRAP_SECRET', 'Process')
$oldLocalApp = [Environment]::GetEnvironmentVariable('LOCALAPPDATA', 'Process')
$oldDbPath = [Environment]::GetEnvironmentVariable('ANXINBOARD_DB_PATH', 'Process')
$oldRestoreAuthority = [Environment]::GetEnvironmentVariable('ANXINBOARD_OFFLINE_RESTORE_AUTHORITY', 'Process')
$oldInstallerTestMode = [Environment]::GetEnvironmentVariable('ANXINBOARD_INSTALLER_TEST_MODE', 'Process')
# Keep the disposable install shallow: PyInstaller's native DLL discovery can hit
# Windows path limits after the versions/<digest> layout is appended. Preserve the
# full random identity and installed version layout, but spend no path budget on labels.
$testLocalApp = Join-Path $env:TEMP ([Guid]::NewGuid().ToString('N'))
$testDataRoot = Join-Path $testLocalApp 'AnxinBoard'
$testDbPath = Join-Path $testDataRoot 'anxinboard.db'
$testInstallRoot = Join-Path $testLocalApp 'i'
$payloadDigest = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
$versionRoot = Join-Path (Join-Path $testInstallRoot 'versions') $payloadDigest
$installedRuntimeDir = Join-Path $versionRoot 'AnxinBoard.Runtime'
$longestNativePath = 0
foreach ($entry in @($manifest.files)) {
    if ([string]$entry.path -match '\.(exe|dll|pyd)$') {
        $installedPath = [System.IO.Path]::GetFullPath((Join-Path $installedRuntimeDir ([string]$entry.path).Replace('/', '\')))
        $longestNativePath = [Math]::Max($longestNativePath, $installedPath.Length)
    }
}
if ($longestNativePath -gt 240) {
    throw "Smoke native paths exceed the 240-character budget ($longestNativePath). Set TEMP to a shorter isolated directory."
}
Write-Host "SMOKE_NATIVE_PATH_MAX=$longestNativePath"
New-Item -ItemType Directory -Path $testLocalApp | Out-Null
$process = $null
$backupPath = Join-Path $testLocalApp 'downloaded-product-backup.zip'

function Wait-HealthyRuntime {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][string]$Origin
    )
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "$Origin/api/health" -Method Get -TimeoutSec 2
            if ($health.status -eq 'ok') { return }
        } catch {
            Start-Sleep -Milliseconds 500
        }
        if ($Process.HasExited) { throw "runtime exited before health check: $($Process.ExitCode)" }
    }
    throw 'runtime did not become healthy within 45 seconds'
}

function Stop-SmokeRuntime {
    param([Parameter(Mandatory = $true)]$Process)
    if (-not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction Stop
        try { [void]$Process.WaitForExit(5000) } catch { }
    }
}

try {
    [Environment]::SetEnvironmentVariable('ANXINBOARD_LOCAL_BOOTSTRAP_SECRET', $bootstrap, 'Process')
    [Environment]::SetEnvironmentVariable('LOCALAPPDATA', $testLocalApp, 'Process')
    [Environment]::SetEnvironmentVariable('ANXINBOARD_DB_PATH', $testDbPath, 'Process')
    [Environment]::SetEnvironmentVariable('ANXINBOARD_OFFLINE_RESTORE_AUTHORITY', $null, 'Process')
    [Environment]::SetEnvironmentVariable('ANXINBOARD_INSTALLER_TEST_MODE', $null, 'Process')
    $process = Start-Process -FilePath $runtimeExe -ArgumentList @('--port', "$port") -WorkingDirectory $runtimeDir -PassThru -WindowStyle Hidden

    Wait-HealthyRuntime -Process $process -Origin $origin
    if (-not (Test-Path -LiteralPath $testDbPath -PathType Leaf)) {
        throw 'packaged runtime did not initialize the explicit isolated product database'
    }
    $dbLength = (Get-Item -LiteralPath $testDbPath).Length
    if ($dbLength -le 0) {
        throw 'packaged runtime initialized an empty isolated product database file'
    }

    $index = Invoke-WebRequest -UseBasicParsing -Uri "$origin/" -Method Get -TimeoutSec 5
    if ($index.StatusCode -ne 200 -or $index.Content -notmatch '<div id="app"></div>') {
        throw 'packaged frontend root did not return the built Vue shell'
    }

    $headers = @{ Origin = $origin }
    $body = @{ bootstrap_secret = $bootstrap } | ConvertTo-Json -Compress
    $session = Invoke-RestMethod -Uri "$origin/api/local-session/exchange" -Method Post -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 5
    if ($session.schema_version -ne 'local_browser_session_v1' -or $session.session_state -ne 'active') {
        throw 'same-origin bootstrap exchange did not establish a local session'
    }
    $sessionToken = [string]$session.session_token
    if ([string]::IsNullOrWhiteSpace($sessionToken)) {
        throw 'local session token was not returned'
    }

    $backupHeaders = @{
        Origin = $origin
        'X-Anxin-Session' = $sessionToken
        'X-Request-ID' = 'lane-c-packaged-backup-smoke'
    }
    Invoke-WebRequest -UseBasicParsing -Uri "$origin/api/product-backup/export" -Method Post -Headers $backupHeaders -OutFile $backupPath -TimeoutSec 20 | Out-Null
    if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) { throw 'guarded packaged backup export did not produce a ZIP' }

    Add-Type -AssemblyName System.IO.Compression
    $backupStream = $null
    $backupArchive = $null
    try {
        $backupStream = [System.IO.File]::OpenRead($backupPath)
        $backupArchive = [System.IO.Compression.ZipArchive]::new($backupStream, [System.IO.Compression.ZipArchiveMode]::Read, $false)
        $names = @($backupArchive.Entries | ForEach-Object FullName)
        if ($names.Count -ne 2 -or $names -notcontains 'manifest.json' -or $names -notcontains 'data/anxinboard.db') {
            throw "packaged backup export member set mismatch: $($names -join ',')"
        }
        $manifestEntry = $backupArchive.GetEntry('manifest.json')
        if (-not $manifestEntry) { throw 'packaged backup manifest is missing' }
        $reader = New-Object System.IO.StreamReader($manifestEntry.Open(), [System.Text.Encoding]::UTF8)
        try { $backupManifestText = $reader.ReadToEnd() } finally { $reader.Dispose() }
        $backupManifest = $backupManifestText | ConvertFrom-Json
        if ($backupManifest.schema_version -ne 'rd_agent_backup_package_v1') { throw 'packaged backup schema mismatch' }
        if (@($backupManifest.entries).Count -ne 1) { throw 'packaged backup must contain one product database entry' }
        $entry = @($backupManifest.entries)[0]
        if ($entry.logical_type -ne 'sqlite_database' -or $entry.source_root_id -ne 'anxinboard-data-v1' -or $entry.archive_path -ne 'data/anxinboard.db') {
            throw 'packaged backup entry does not match the product database contract'
        }
        if ($backupManifestText -match [Regex]::Escape($sessionToken) -or $backupManifestText -match [Regex]::Escape('lane-c-ci-bootstrap-not-a-real-secret-2026')) {
            throw 'packaged backup manifest leaked local session material'
        }
    } finally {
        if ($backupArchive) { $backupArchive.Dispose() }
        if ($backupStream) { $backupStream.Dispose() }
    }

    # Close the packaged product and remove only the isolated product DB. The smoke then
    # constructs an isolated installed-product layout from this exact payload and invokes
    # the same nontechnical restore controller shipped to the client. This exercises the
    # controller lifecycle lock + installed-runtime integrity resolver + packaged offline
    # restore without touching a real user installation or credential store.
    Stop-SmokeRuntime -Process $process
    $process = $null
    foreach ($sidecar in @("$testDbPath-wal", "$testDbPath-shm", "$testDbPath-journal")) {
        if (Test-Path -LiteralPath $sidecar) { throw "packaged runtime left SQLite sidecar after stop: $sidecar" }
    }
    [System.IO.File]::Delete($testDbPath)
    if (Test-Path -LiteralPath $testDbPath) { throw 'isolated product database could not be removed before restore smoke' }

    New-Item -ItemType Directory -Path $versionRoot -Force | Out-Null
    Copy-Item -LiteralPath $runtimeDir -Destination $versionRoot -Recurse -Force
    Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $versionRoot 'runtime-manifest.json') -Force
    New-Item -ItemType Directory -Path $testInstallRoot -Force | Out-Null
    ([ordered]@{
        schema_version = 'anxin_windows_install_state_v1'
        current_payload_digest = $payloadDigest
    } | ConvertTo-Json -Compress) | Set-Content -LiteralPath (Join-Path $testInstallRoot 'install-state.json') -Encoding utf8

    [Environment]::SetEnvironmentVariable('ANXINBOARD_INSTALLER_TEST_MODE', '1', 'Process')
    powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $restoreScriptPath `
        -BackupPath $backupPath `
        -ConfirmRestore `
        -TestInstallRoot $testInstallRoot `
        -TestDataRoot $testDataRoot `
        -NonInteractive
    $restoreExit = $LASTEXITCODE
    [Environment]::SetEnvironmentVariable('ANXINBOARD_INSTALLER_TEST_MODE', $null, 'Process')
    if ($restoreExit -ne 0) { throw "user restore controller failed with exit $restoreExit" }
    if (-not (Test-Path -LiteralPath $testDbPath -PathType Leaf)) { throw 'user restore controller did not recreate the isolated database' }
    if ((Get-Item -LiteralPath $testDbPath).Length -le 0) { throw 'user restore controller recreated an empty database' }

    $process = Start-Process -FilePath $runtimeExe -ArgumentList @('--port', "$port") -WorkingDirectory $runtimeDir -PassThru -WindowStyle Hidden
    Wait-HealthyRuntime -Process $process -Origin $origin

    Write-Host 'WINDOWS_RUNTIME_SMOKE=SUCCESS'
    Write-Host 'HEALTH=SUCCESS'
    Write-Host 'EXPLICIT_ISOLATED_DATABASE=SUCCESS'
    Write-Host 'STATIC_UI=SUCCESS'
    Write-Host 'SAME_ORIGIN_SESSION_EXCHANGE=SUCCESS'
    Write-Host 'GUARDED_PRODUCT_BACKUP_EXPORT=SUCCESS'
    Write-Host 'USER_RESTORE_CONTROLLER_E2E=SUCCESS'
    Write-Host 'PACKAGED_OFFLINE_RESTORE=SUCCESS'
    Write-Host 'RESTORED_RUNTIME_RESTART=SUCCESS'
} finally {
    [Environment]::SetEnvironmentVariable('ANXINBOARD_LOCAL_BOOTSTRAP_SECRET', $oldBootstrap, 'Process')
    [Environment]::SetEnvironmentVariable('LOCALAPPDATA', $oldLocalApp, 'Process')
    [Environment]::SetEnvironmentVariable('ANXINBOARD_DB_PATH', $oldDbPath, 'Process')
    [Environment]::SetEnvironmentVariable('ANXINBOARD_OFFLINE_RESTORE_AUTHORITY', $oldRestoreAuthority, 'Process')
    [Environment]::SetEnvironmentVariable('ANXINBOARD_INSTALLER_TEST_MODE', $oldInstallerTestMode, 'Process')
    $bootstrap = $null
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        try { [void]$process.WaitForExit(5000) } catch { }
    }
    if (Test-Path -LiteralPath $testLocalApp) {
        [System.IO.Directory]::Delete($testLocalApp, $true)
    }
}
