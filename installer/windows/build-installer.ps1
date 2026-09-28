[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadRoot,
    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,
    [string]$ISCCPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$installerAppId = '{06AEBA51-E4DC-4F31-83FE-790A9BCE45CB}'
$gitRuntimePrerequisite = 'Git for Windows 2.45+ with --no-lazy-fetch capability'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$issPath = Join-Path $PSScriptRoot 'AnxinBoard.iss'

function Assert-PlainPath {
    param([string]$Path, [switch]$AllowMissing)
    if (-not (Test-Path -LiteralPath $Path)) {
        if ($AllowMissing) { return }
        throw "Required installer path is missing: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Installer input/output contains a reparse point.'
    }
    return $item
}

function Resolve-ManifestFile {
    param([string]$RuntimeRoot, [string]$Relative)
    if ([string]::IsNullOrWhiteSpace($Relative) -or [System.IO.Path]::IsPathRooted($Relative)) {
        throw 'Runtime manifest path is invalid.'
    }
    $portable = $Relative.Replace('/', '\')
    $parts = $portable.Split('\')
    if ($parts | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' }) {
        throw 'Runtime manifest path is invalid.'
    }
    $rootFull = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\') + '\'
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot $portable))
    if (-not $candidate.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Runtime manifest path escapes payload root.'
    }
    return $candidate
}

if (-not [System.IO.Path]::IsPathRooted($PayloadRoot)) {
    $PayloadRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $PayloadRoot))
}
$PayloadRoot = [System.IO.Path]::GetFullPath($PayloadRoot)
if (-not [System.IO.Path]::IsPathRooted($OutputRoot)) {
    $OutputRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputRoot))
}
$OutputRoot = [System.IO.Path]::GetFullPath($OutputRoot)
if ($PayloadRoot -match '^(\\\\|//)' -or $OutputRoot -match '^(\\\\|//)') {
    throw 'Installer payload/output must be local paths.'
}

$payloadItem = Assert-PlainPath -Path $PayloadRoot
if (-not $payloadItem.PSIsContainer) { throw 'PayloadRoot must be a directory.' }
Assert-PlainPath -Path $issPath | Out-Null
foreach ($tool in @(
    'install-runtime.ps1',
    'rollback-runtime.ps1',
    'uninstall-product.ps1',
    'resolve-installed-runtime.ps1',
    'start-installed-product.ps1',
    'stop-installed-product.ps1',
    'restore-product-backup.ps1'
)) {
    $item = Assert-PlainPath -Path (Join-Path $PSScriptRoot $tool)
    if ($item.PSIsContainer) { throw "Installer lifecycle tool is not a file: $tool" }
}

$runtimeManifestPath = Join-Path $PayloadRoot 'runtime-manifest.json'
$runtimeRoot = Join-Path $PayloadRoot 'AnxinBoard.Runtime'
$manifestItem = Assert-PlainPath -Path $runtimeManifestPath
$runtimeItem = Assert-PlainPath -Path $runtimeRoot
if ($manifestItem.PSIsContainer -or -not $runtimeItem.PSIsContainer) { throw 'Runtime payload shape is invalid.' }
$runtimeManifest = Get-Content -LiteralPath $runtimeManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($runtimeManifest.schema_version -ne 'anxin_windows_runtime_payload_v1') { throw 'Runtime payload schema is unsupported.' }
$sourceCommit = [string]$runtimeManifest.source_commit
if ($sourceCommit -notmatch '^[0-9a-f]{40}$') { throw 'Runtime payload source commit is invalid.' }
$repoHead = (git -C $repoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $repoHead -ne $sourceCommit) {
    throw "Runtime payload is not bound to exact current Product HEAD: payload=$sourceCommit repo=$repoHead"
}

$manifestPaths = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($entry in @($runtimeManifest.files)) {
    $relative = [string]$entry.path
    if (-not $manifestPaths.Add($relative)) { throw 'Runtime manifest contains duplicate path identity.' }
    if ([string]$entry.sha256 -notmatch '^[0-9a-f]{64}$') { throw "Runtime manifest digest is invalid: $relative" }
    $candidate = Resolve-ManifestFile -RuntimeRoot $runtimeRoot -Relative $relative
    $item = Assert-PlainPath -Path $candidate
    if ($item.PSIsContainer) { throw "Runtime manifest member is not a file: $relative" }
    if ([int64]$item.Length -ne [int64]$entry.size) { throw "Runtime manifest size mismatch: $relative" }
    $digest = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($digest -ne ([string]$entry.sha256).ToLowerInvariant()) { throw "Runtime manifest digest mismatch: $relative" }
}
if ($manifestPaths.Count -eq 0) { throw 'Runtime manifest contains no files.' }
foreach ($binding in @('runtime', 'frontend', 'diagnostics')) {
    $relative = [string]$runtimeManifest.$binding
    if ([string]::IsNullOrWhiteSpace($relative) -or -not $manifestPaths.Contains($relative)) {
        throw "Runtime manifest $binding binding is not integrity-bound."
    }
}

if ([string]::IsNullOrWhiteSpace($ISCCPath)) {
    $command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($command) { $ISCCPath = [string]$command.Path }
    if ([string]::IsNullOrWhiteSpace($ISCCPath) -and ${env:ProgramFiles(x86)}) {
        $candidate = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $ISCCPath = $candidate }
    }
    if ([string]::IsNullOrWhiteSpace($ISCCPath) -and $env:ProgramFiles) {
        $candidate = Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $ISCCPath = $candidate }
    }
}
if ([string]::IsNullOrWhiteSpace($ISCCPath)) { throw 'Inno Setup 6 ISCC.exe is not available.' }
if (-not [System.IO.Path]::IsPathRooted($ISCCPath)) { throw 'ISCCPath must be absolute.' }
$ISCCPath = [System.IO.Path]::GetFullPath($ISCCPath)
$isccItem = Assert-PlainPath -Path $ISCCPath
if ($isccItem.PSIsContainer) { throw 'ISCCPath is not a file.' }

New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
$outputItem = Assert-PlainPath -Path $OutputRoot
if (-not $outputItem.PSIsContainer) { throw 'OutputRoot must be a directory.' }
if (@(Get-ChildItem -LiteralPath $OutputRoot -Force).Count -ne 0) {
    throw 'Installer OutputRoot must start empty.'
}

$launcherRoot = Join-Path $OutputRoot 'desktop-launcher'
& (Join-Path $PSScriptRoot 'build-launcher.ps1') -OutputRoot $launcherRoot

$shortCommit = $sourceCommit.Substring(0, 12)
$setupName = "AnxinBoard-Setup-candidate-$shortCommit.exe"
$setupPath = Join-Path $OutputRoot $setupName
$arguments = @(
    '/Qp',
    "/DPayloadRoot=$PayloadRoot",
    "/DLauncherRoot=$launcherRoot",
    "/DOutputRoot=$OutputRoot",
    "/DSourceCommit=$sourceCommit",
    "/DSourceCommitShort=$shortCommit",
    $issPath
)
& $ISCCPath @arguments
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed with exit code $LASTEXITCODE." }
if (-not (Test-Path -LiteralPath $setupPath -PathType Leaf)) { throw 'Expected Inno Setup candidate was not produced.' }

$setupItem = Assert-PlainPath -Path $setupPath
$setupDigest = (Get-FileHash -LiteralPath $setupPath -Algorithm SHA256).Hash.ToLowerInvariant()
$payloadManifestDigest = (Get-FileHash -LiteralPath $runtimeManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
$signature = Get-AuthenticodeSignature -LiteralPath $setupPath
$manifest = [ordered]@{
    schema_version = 'anxin_windows_inno_installer_candidate_v1'
    formal_release = $false
    release_blocker = 'real-world candidate; clean Windows evidence, Authenticode, and independent installed-launcher acceptance remain pending'
    source_commit = $sourceCommit
    payload_manifest_sha256 = $payloadManifestDigest
    installer_engine = 'Inno Setup 6'
    installer_compiler = $ISCCPath
    installer_app_id = $installerAppId
    install_scope = 'current_user'
    product_program_root = '%LOCALAPPDATA%\Programs\AnxinBoard'
    installer_control_root = '%LOCALAPPDATA%\Programs\AnxinBoard Installer Control'
    user_data_root = '%LOCALAPPDATA%\AnxinBoard'
    user_data_uninstall_semantics = 'preserved'
    launcher_shortcut = 'desktop_and_start_menu_native_secure_launcher'
    launcher_sha256 = (Get-FileHash -LiteralPath (Join-Path $launcherRoot '安心看板.exe') -Algorithm SHA256).Hash.ToLowerInvariant()
    launcher_icon_sha256 = (Get-FileHash -LiteralPath (Join-Path $launcherRoot 'AnxinBoard.ico') -Algorithm SHA256).Hash.ToLowerInvariant()
    stop_shortcut = 'start_menu_fail_closed_installed_stop_candidate'
    normal_user_machine_prerequisites = @($gitRuntimePrerequisite)
    setup_file = $setupName
    setup_bytes = [int64]$setupItem.Length
    setup_sha256 = $setupDigest
    authenticode_status = [string]$signature.Status
    real_install_executed = $false
    real_uninstall_executed = $false
    real_credential_access = $false
    real_user_data_access = $false
}
$manifestPath = Join-Path $OutputRoot 'installer-candidate-manifest.json'
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding utf8

Write-Host 'WINDOWS_INNO_INSTALLER_CANDIDATE=BUILT_NOT_INSTALLED_NOT_RELEASED'
Write-Host "INNO_SETUP=$setupPath"
Write-Host "INNO_SETUP_SHA256=$setupDigest"
Write-Host "INNO_AUTHENTICODE_STATUS=$($signature.Status)"
Write-Host 'INSTALLED_LAUNCHER_SHORTCUT=INCLUDED_CANDIDATE'
Write-Host 'REAL_INSTALL_EXECUTED=NO'
Write-Host 'REAL_UNINSTALL_EXECUTED=NO'
Write-Host 'REAL_CREDENTIAL_ACCESS=NO'
Write-Host 'REAL_USER_DATA_ACCESS=NO'
