[CmdletBinding()]
param([string]$TestInstallRoot)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-InstallRoot {
    param([string]$TestRoot)
    if (-not [string]::IsNullOrWhiteSpace($TestRoot)) {
        if ($env:ANXINBOARD_INSTALLER_TEST_MODE -ne '1') { throw 'TestInstallRoot is available only in explicit installer test mode.' }
        if (-not [System.IO.Path]::IsPathRooted($TestRoot)) { throw 'TestInstallRoot must be absolute.' }
        $root = [System.IO.Path]::GetFullPath($TestRoot)
    } else {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is unavailable.' }
        $root = Join-Path $env:LOCALAPPDATA 'Programs\AnxinBoard'
    }
    if ($root -match '^(\\\\|//)') { throw 'Install root must be local.' }
    return $root
}

function Assert-PlainPath {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { throw 'Installed runtime path is incomplete.' }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Installed runtime contains a reparse point.' }
    return $item
}

function Assert-NoReparseExistingAncestorChain {
    param([string]$Path)
    $current = [System.IO.DirectoryInfo]::new([System.IO.Path]::GetFullPath($Path))
    while ($null -ne $current) {
        if (Test-Path -LiteralPath $current.FullName) {
            $item = Get-Item -LiteralPath $current.FullName -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Installed runtime path ancestor contains a reparse point.'
            }
        }
        $current = $current.Parent
    }
}

function Resolve-ManifestMember {
    param([string]$RuntimeRoot, [string]$Relative)
    if ([string]::IsNullOrWhiteSpace($Relative) -or [System.IO.Path]::IsPathRooted($Relative)) { throw 'Installed runtime manifest path is invalid.' }
    $portable = $Relative.Replace('/', '\')
    if ($portable.Split('\') | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' }) { throw 'Installed runtime manifest path is invalid.' }
    $rootFull = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\') + '\'
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot $portable))
    if (-not $candidate.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Installed runtime manifest path escapes runtime root.' }
    return $candidate
}

function Assert-NoReparseChain {
    param([string]$Root, [string]$Relative)
    $current = [System.IO.Path]::GetFullPath($Root)
    Assert-PlainPath -Path $current | Out-Null
    foreach ($part in $Relative.Replace('/', '\').Split('\')) {
        $current = Join-Path $current $part
        Assert-PlainPath -Path $current | Out-Null
    }
}

function Get-ActualRuntimeFiles {
    param([string]$RuntimeRoot)
    $rootFull = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    $rootItem = Assert-PlainPath -Path $rootFull
    if (-not $rootItem.PSIsContainer) { throw 'Installed runtime root is not a directory.' }
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push($rootFull)
    $set = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Installed runtime contains a reparse point.' }
            if ($item.PSIsContainer) { $pending.Push($item.FullName); continue }
            $relative = $item.FullName.Substring($rootFull.Length + 1).Replace('\', '/')
            if (-not $set.Add($relative)) { throw 'Installed runtime contains duplicate file identity.' }
        }
    }
    return $set
}

$installRoot = Resolve-InstallRoot -TestRoot $TestInstallRoot
Assert-NoReparseExistingAncestorChain -Path $installRoot
$rootItem = Assert-PlainPath -Path $installRoot
if (-not $rootItem.PSIsContainer) { throw 'Install root is not a directory.' }
$statePath = Join-Path $installRoot 'install-state.json'
$stateItem = Assert-PlainPath -Path $statePath
if ($stateItem.PSIsContainer) { throw 'Install state is not a file.' }
$state = Get-Content -LiteralPath $statePath -Raw -Encoding utf8 | ConvertFrom-Json
if ($state.schema_version -ne 'anxin_windows_install_state_v1') { throw 'Install state schema is unsupported.' }
$digest = [string]$state.current_payload_digest
if ($digest -notmatch '^[0-9a-f]{64}$') { throw 'Current payload digest is invalid.' }

$versionRoot = Join-Path (Join-Path $installRoot 'versions') $digest
$manifestPath = Join-Path $versionRoot 'runtime-manifest.json'
$runtimeRoot = Join-Path $versionRoot 'AnxinBoard.Runtime'
$manifestItem = Assert-PlainPath -Path $manifestPath
$runtimeItem = Assert-PlainPath -Path $runtimeRoot
if ($manifestItem.PSIsContainer -or -not $runtimeItem.PSIsContainer) { throw 'Installed payload shape is invalid.' }
$manifestDigest = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($manifestDigest -ne $digest) { throw 'Installed runtime manifest digest does not match active pointer.' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($manifest.schema_version -ne 'anxin_windows_runtime_payload_v1') { throw 'Installed runtime manifest schema is unsupported.' }

$expected = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($entry in @($manifest.files)) {
    $relative = [string]$entry.path
    if (-not $expected.Add($relative)) { throw 'Installed runtime manifest contains duplicate path identity.' }
    if ([string]$entry.sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Installed runtime member digest is invalid.' }
    $candidate = Resolve-ManifestMember -RuntimeRoot $runtimeRoot -Relative $relative
    Assert-NoReparseChain -Root $runtimeRoot -Relative $relative
    $item = Get-Item -LiteralPath $candidate -Force
    if ($item.PSIsContainer) { throw 'Installed runtime manifest member is not a file.' }
    if ([int64]$item.Length -ne [int64]$entry.size) { throw "Installed runtime member size mismatch: $relative" }
    $memberDigest = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($memberDigest -ne ([string]$entry.sha256).ToLowerInvariant()) { throw "Installed runtime member digest mismatch: $relative" }
}
$actual = Get-ActualRuntimeFiles -RuntimeRoot $runtimeRoot
if ($actual.Count -ne $expected.Count) { throw 'Installed runtime member set does not match manifest.' }
foreach ($relative in $actual) { if (-not $expected.Contains($relative)) { throw 'Installed runtime contains an unmanifested file.' } }

foreach ($binding in @('runtime', 'frontend', 'diagnostics')) {
    $relative = [string]$manifest.$binding
    if ([string]::IsNullOrWhiteSpace($relative) -or -not $expected.Contains($relative)) {
        throw "Installed runtime $binding member is not integrity-bound."
    }
}
$runtimeRelative = [string]$manifest.runtime
$null = Resolve-ManifestMember -RuntimeRoot $runtimeRoot -Relative $runtimeRelative

# This seam intentionally does not start/stop processes, acquire the #130 lifecycle
# lock, mint/exchange bootstrap material, or access Credential Manager. It only resolves
# and attests the active installed runtime for the future accepted launcher authority.
Write-Output ([ordered]@{
    schema_version = 'anxin_installed_runtime_resolution_v1'
    payload_digest = $digest
    source_commit = [string]$manifest.source_commit
    runtime_executable_relative = "versions/$digest/AnxinBoard.Runtime/$runtimeRelative"
    launcher_authority = 'not_performed'
    credential_access = 'not_performed'
} | ConvertTo-Json -Compress)
