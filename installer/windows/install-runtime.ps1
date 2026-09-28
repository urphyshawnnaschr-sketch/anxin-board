[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadRoot,
    [string]$TestInstallRoot
)

$ErrorActionPreference = 'Stop'
# Match the native launcher: inherited PowerShell 7 module paths are not valid
# for this Windows PowerShell entry point. Set only this process's search path.
$env:PSModulePath = [System.IO.Path]::Combine(
    [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::System),
    'WindowsPowerShell\v1.0\Modules')
Set-StrictMode -Version Latest

function Resolve-InstallRoot {
    param([string]$TestRoot)
    if (-not [string]::IsNullOrWhiteSpace($TestRoot)) {
        if ($env:ANXINBOARD_INSTALLER_TEST_MODE -ne '1') {
            throw 'TestInstallRoot is available only in explicit installer test mode.'
        }
        if (-not [System.IO.Path]::IsPathRooted($TestRoot)) { throw 'TestInstallRoot must be absolute.' }
        $resolved = [System.IO.Path]::GetFullPath($TestRoot)
    } else {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is unavailable.' }
        $resolved = Join-Path $env:LOCALAPPDATA 'Programs\AnxinBoard'
    }
    if ($resolved -match '^(\\\\|//)') { throw 'Install root must be a local path.' }
    return $resolved
}

function Assert-PlainPath {
    param([string]$Path, [switch]$AllowMissing)
    if (-not (Test-Path -LiteralPath $Path)) {
        if ($AllowMissing) { return }
        throw "Required path is missing: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Reparse points are outside the installer contract.'
    }
    return $item
}

function Assert-NoReparseExistingAncestorChain {
    param([string]$Path)
    $current = [System.IO.DirectoryInfo]::new([System.IO.Path]::GetFullPath($Path))
    while ($null -ne $current) {
        if (Test-Path -LiteralPath $current.FullName) {
            $item = Get-Item -LiteralPath $current.FullName -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Install path ancestor contains a reparse point.'
            }
        }
        $current = $current.Parent
    }
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

function Assert-NoReparseRelativeChain {
    param([string]$Root, [string]$Relative)
    $current = [System.IO.Path]::GetFullPath($Root)
    Assert-PlainPath -Path $current | Out-Null
    foreach ($part in $Relative.Replace('/', '\').Split('\')) {
        $current = Join-Path $current $part
        Assert-PlainPath -Path $current | Out-Null
    }
}

function Get-RuntimeFileSet {
    param([string]$RuntimeRoot)
    $root = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    Assert-PlainPath -Path $root | Out-Null
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push($root)
    $files = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Runtime payload contains a reparse point.'
            }
            if ($item.PSIsContainer) {
                $pending.Push($item.FullName)
                continue
            }
            $relative = $item.FullName.Substring($root.Length + 1).Replace('\', '/')
            if (-not $files.Add($relative)) { throw 'Runtime payload contains duplicate file identity.' }
        }
    }
    return $files
}

function Assert-RuntimeTree {
    param($Manifest, [string]$RuntimeRoot)
    Assert-PlainPath -Path $RuntimeRoot | Out-Null
    $entries = @($Manifest.files)
    if ($entries.Count -eq 0) { throw 'Runtime payload manifest has no files.' }

    $manifestFiles = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $entries) {
        $relative = [string]$entry.path
        if (-not $manifestFiles.Add($relative)) { throw 'Runtime manifest contains duplicate path identity.' }
        if ([string]$entry.sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Runtime manifest member digest is invalid.' }
        $candidate = Resolve-ManifestFile -RuntimeRoot $RuntimeRoot -Relative $relative
        Assert-NoReparseRelativeChain -Root $RuntimeRoot -Relative $relative
        $item = Get-Item -LiteralPath $candidate -Force
        if ($item.PSIsContainer) { throw "Runtime manifest member is not a file: $relative" }
        if ([int64]$item.Length -ne [int64]$entry.size) { throw "Runtime file size mismatch: $relative" }
        $digest = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($digest -ne ([string]$entry.sha256).ToLowerInvariant()) {
            throw "Runtime file digest mismatch: $relative"
        }
    }

    $actualFiles = Get-RuntimeFileSet -RuntimeRoot $RuntimeRoot
    if ($actualFiles.Count -ne $manifestFiles.Count) { throw 'Runtime payload member set does not match manifest.' }
    foreach ($relative in $actualFiles) {
        if (-not $manifestFiles.Contains($relative)) { throw 'Runtime payload contains an unmanifested file.' }
    }

    foreach ($binding in @('runtime', 'frontend', 'diagnostics')) {
        $relative = [string]$Manifest.$binding
        if ([string]::IsNullOrWhiteSpace($relative) -or -not $manifestFiles.Contains($relative)) {
            throw "Runtime manifest $binding member is not integrity-bound."
        }
    }
}

if (-not [System.IO.Path]::IsPathRooted($PayloadRoot)) {
    $PayloadRoot = [System.IO.Path]::GetFullPath($PayloadRoot)
}
$PayloadRoot = [System.IO.Path]::GetFullPath($PayloadRoot)
Assert-PlainPath -Path $PayloadRoot | Out-Null
$manifestPath = Join-Path $PayloadRoot 'runtime-manifest.json'
$runtimeSource = Join-Path $PayloadRoot 'AnxinBoard.Runtime'
$manifestItem = Assert-PlainPath -Path $manifestPath
$runtimeItem = Assert-PlainPath -Path $runtimeSource
if ($manifestItem.PSIsContainer -or -not $runtimeItem.PSIsContainer) { throw 'Runtime payload shape is invalid.' }

$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($manifest.schema_version -ne 'anxin_windows_runtime_payload_v1') { throw 'Runtime payload schema is unsupported.' }
Assert-RuntimeTree -Manifest $manifest -RuntimeRoot $runtimeSource
$payloadDigest = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()

$installRoot = Resolve-InstallRoot -TestRoot $TestInstallRoot
Assert-NoReparseExistingAncestorChain -Path $installRoot
Assert-PlainPath -Path $installRoot -AllowMissing
New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
Assert-PlainPath -Path $installRoot | Out-Null
$versionsRoot = Join-Path $installRoot 'versions'
if (Test-Path -LiteralPath $versionsRoot) { Assert-PlainPath -Path $versionsRoot | Out-Null }
New-Item -ItemType Directory -Path $versionsRoot -Force | Out-Null
Assert-PlainPath -Path $versionsRoot | Out-Null

$targetVersion = Join-Path $versionsRoot $payloadDigest
if (Test-Path -LiteralPath $targetVersion) {
    Assert-PlainPath -Path $targetVersion | Out-Null
    $installedManifestPath = Join-Path $targetVersion 'runtime-manifest.json'
    $installedManifestItem = Assert-PlainPath -Path $installedManifestPath
    if ($installedManifestItem.PSIsContainer) { throw 'Existing installed version manifest is invalid.' }
    $installedDigest = (Get-FileHash -LiteralPath $installedManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($installedDigest -ne $payloadDigest) { throw 'Existing installed version manifest is inconsistent.' }
    $installedRuntimeRoot = Join-Path $targetVersion 'AnxinBoard.Runtime'
    Assert-RuntimeTree -Manifest $manifest -RuntimeRoot $installedRuntimeRoot
} else {
    $staging = Join-Path $versionsRoot ('.staging-' + [Guid]::NewGuid().ToString('N'))
    try {
        New-Item -ItemType Directory -Path $staging | Out-Null
        Assert-PlainPath -Path $staging | Out-Null
        Copy-Item -LiteralPath $runtimeSource -Destination (Join-Path $staging 'AnxinBoard.Runtime') -Recurse
        Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $staging 'runtime-manifest.json')
        $stagedRuntimeRoot = Join-Path $staging 'AnxinBoard.Runtime'
        Assert-RuntimeTree -Manifest $manifest -RuntimeRoot $stagedRuntimeRoot
        $stagedDigest = (Get-FileHash -LiteralPath (Join-Path $staging 'runtime-manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($stagedDigest -ne $payloadDigest) { throw 'Staged runtime manifest changed during install.' }
        Move-Item -LiteralPath $staging -Destination $targetVersion
        Assert-PlainPath -Path $targetVersion | Out-Null
    } finally {
        if (Test-Path -LiteralPath $staging) { [System.IO.Directory]::Delete($staging, $true) }
    }
}

$statePath = Join-Path $installRoot 'install-state.json'
$previousDigest = $null
$stateObjects = @(
    Get-ChildItem -LiteralPath $installRoot -Force |
        Where-Object { $_.Name -ieq 'install-state.json' }
)
if ($stateObjects.Count -gt 1) { throw 'Install state identity is ambiguous.' }
if ($stateObjects.Count -eq 1) {
    $stateItem = $stateObjects[0]
    if (($stateItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or $stateItem.PSIsContainer) {
        throw 'Existing install state is not a plain file; refusing to overwrite it.'
    }
    try {
        $currentState = Get-Content -LiteralPath $stateItem.FullName -Raw -Encoding utf8 | ConvertFrom-Json
        if ($currentState.schema_version -ne 'anxin_windows_install_state_v1') { throw 'Installed state schema is unsupported.' }
        $oldCurrent = [string]$currentState.current_payload_digest
        if ($oldCurrent -and $oldCurrent -notmatch '^[0-9a-f]{64}$') { throw 'Installed current payload digest is invalid.' }
        if ($oldCurrent -and $oldCurrent -ne $payloadDigest) { $previousDigest = $oldCurrent }
        elseif ($currentState.PSObject.Properties.Name -contains 'previous_payload_digest') {
            $candidatePrevious = [string]$currentState.previous_payload_digest
            if ($candidatePrevious -and $candidatePrevious -notmatch '^[0-9a-f]{64}$') { throw 'Installed previous payload digest is invalid.' }
            $previousDigest = $candidatePrevious
        }
    } catch {
        throw 'Existing install state is invalid; refusing to overwrite it.'
    }
}

$state = [ordered]@{
    schema_version = 'anxin_windows_install_state_v1'
    current_payload_digest = $payloadDigest
    previous_payload_digest = $previousDigest
    source_commit = [string]$manifest.source_commit
    runtime_relative = "versions/$payloadDigest/AnxinBoard.Runtime"
    manifest_relative = "versions/$payloadDigest/runtime-manifest.json"
    updated_at = [DateTimeOffset]::UtcNow.ToString('o')
}
$tempState = Join-Path $installRoot ('.install-state-' + [Guid]::NewGuid().ToString('N') + '.json')
try {
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $tempState -Encoding utf8
    $tempItem = Assert-PlainPath -Path $tempState
    if ($tempItem.PSIsContainer) { throw 'Temporary install state is not a file.' }
    Move-Item -LiteralPath $tempState -Destination $statePath -Force
    $writtenState = Assert-PlainPath -Path $statePath
    if ($writtenState.PSIsContainer) { throw 'Written install state is not a file.' }
} finally {
    if (Test-Path -LiteralPath $tempState) { [System.IO.File]::Delete($tempState) }
}

Write-Host 'ANXIN_INSTALL=SUCCESS'
Write-Host "PAYLOAD_DIGEST=$payloadDigest"
Write-Host "INSTALL_ROOT=$installRoot"
