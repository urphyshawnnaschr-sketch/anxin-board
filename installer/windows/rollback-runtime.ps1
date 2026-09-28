[CmdletBinding()]
param([string]$TestInstallRoot)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Resolve-InstallRoot {
    param([string]$TestRoot)
    if (-not [string]::IsNullOrWhiteSpace($TestRoot)) {
        if ($env:ANXINBOARD_INSTALLER_TEST_MODE -ne '1') { throw 'TestInstallRoot is available only in explicit installer test mode.' }
        if (-not [System.IO.Path]::IsPathRooted($TestRoot)) { throw 'TestInstallRoot must be absolute.' }
        return [System.IO.Path]::GetFullPath($TestRoot)
    }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is unavailable.' }
    return (Join-Path $env:LOCALAPPDATA 'Programs\AnxinBoard')
}

function Assert-Digest {
    param([string]$Value)
    if ($Value -notmatch '^[0-9a-f]{64}$') { throw 'Installed payload digest is invalid.' }
}

function Assert-PlainPath {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { throw 'Rollback path is incomplete.' }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Rollback target contains a reparse point.'
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
                throw 'Rollback path ancestor contains a reparse point.'
            }
        }
        $current = $current.Parent
    }
}

function Resolve-ManifestFile {
    param([string]$RuntimeRoot, [string]$Relative)
    if ([string]::IsNullOrWhiteSpace($Relative) -or [System.IO.Path]::IsPathRooted($Relative)) {
        throw 'Rollback runtime manifest path is invalid.'
    }
    $portable = $Relative.Replace('/', '\')
    $parts = $portable.Split('\')
    if ($parts | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' }) {
        throw 'Rollback runtime manifest path is invalid.'
    }
    $rootFull = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\') + '\'
    $candidate = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot $portable))
    if (-not $candidate.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Rollback runtime manifest path escapes runtime root.'
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
                throw 'Rollback target contains a reparse point.'
            }
            if ($item.PSIsContainer) {
                $pending.Push($item.FullName)
                continue
            }
            $relative = $item.FullName.Substring($root.Length + 1).Replace('\', '/')
            if (-not $files.Add($relative)) { throw 'Rollback runtime contains duplicate file identity.' }
        }
    }
    return $files
}

function Assert-RuntimeTree {
    param($Manifest, [string]$RuntimeRoot)
    $entries = @($Manifest.files)
    if ($entries.Count -eq 0) { throw 'Rollback runtime manifest has no files.' }

    $manifestFiles = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $entries) {
        $relative = [string]$entry.path
        if (-not $manifestFiles.Add($relative)) { throw 'Rollback runtime manifest contains duplicate path identity.' }
        if ([string]$entry.sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Rollback runtime member digest is invalid.' }
        $candidate = Resolve-ManifestFile -RuntimeRoot $RuntimeRoot -Relative $relative
        Assert-NoReparseRelativeChain -Root $RuntimeRoot -Relative $relative
        $item = Get-Item -LiteralPath $candidate -Force
        if ($item.PSIsContainer) { throw 'Rollback runtime manifest member is not a file.' }
        if ([int64]$item.Length -ne [int64]$entry.size) { throw "Rollback runtime file size mismatch: $relative" }
        $digest = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($digest -ne ([string]$entry.sha256).ToLowerInvariant()) {
            throw "Rollback runtime file digest mismatch: $relative"
        }
    }

    $actualFiles = Get-RuntimeFileSet -RuntimeRoot $RuntimeRoot
    if ($actualFiles.Count -ne $manifestFiles.Count) { throw 'Rollback runtime member set does not match manifest.' }
    foreach ($relative in $actualFiles) {
        if (-not $manifestFiles.Contains($relative)) { throw 'Rollback runtime contains an unmanifested file.' }
    }

    $runtimeRelative = [string]$Manifest.runtime
    if ([string]::IsNullOrWhiteSpace($runtimeRelative) -or -not $manifestFiles.Contains($runtimeRelative)) {
        throw 'Rollback runtime executable is not integrity-bound by the manifest.'
    }
}

$installRoot = Resolve-InstallRoot -TestRoot $TestInstallRoot
Assert-NoReparseExistingAncestorChain -Path $installRoot
$versionsRoot = Join-Path $installRoot 'versions'
$statePath = Join-Path $installRoot 'install-state.json'
Assert-PlainPath -Path $installRoot | Out-Null
Assert-PlainPath -Path $versionsRoot | Out-Null
$stateItem = Assert-PlainPath -Path $statePath
if ($stateItem.PSIsContainer) { throw 'Install state must be a plain file.' }
$state = Get-Content -LiteralPath $statePath -Raw -Encoding utf8 | ConvertFrom-Json
if ($state.schema_version -ne 'anxin_windows_install_state_v1') { throw 'Install state schema is unsupported.' }
$current = [string]$state.current_payload_digest
$previous = [string]$state.previous_payload_digest
Assert-Digest -Value $current
Assert-Digest -Value $previous
if ($current -eq $previous) { throw 'Rollback target must differ from current payload.' }

$previousDir = Join-Path $versionsRoot $previous
$previousManifest = Join-Path $previousDir 'runtime-manifest.json'
$previousRuntimeRoot = Join-Path $previousDir 'AnxinBoard.Runtime'
$previousRuntime = Join-Path $previousRuntimeRoot 'AnxinBoard.Runtime.exe'
Assert-PlainPath -Path $previousDir | Out-Null
$manifestItem = Assert-PlainPath -Path $previousManifest
Assert-PlainPath -Path $previousRuntimeRoot | Out-Null
$runtimeItem = Assert-PlainPath -Path $previousRuntime
if ($manifestItem.PSIsContainer -or $runtimeItem.PSIsContainer) { throw 'Rollback target is incomplete.' }

$manifestDigest = (Get-FileHash -LiteralPath $previousManifest -Algorithm SHA256).Hash.ToLowerInvariant()
if ($manifestDigest -ne $previous) { throw 'Rollback manifest digest does not match install state.' }
$manifest = Get-Content -LiteralPath $previousManifest -Raw -Encoding utf8 | ConvertFrom-Json
if ($manifest.schema_version -ne 'anxin_windows_runtime_payload_v1') { throw 'Rollback payload schema is unsupported.' }
Assert-RuntimeTree -Manifest $manifest -RuntimeRoot $previousRuntimeRoot

$newState = [ordered]@{
    schema_version = 'anxin_windows_install_state_v1'
    current_payload_digest = $previous
    previous_payload_digest = $current
    source_commit = [string]$manifest.source_commit
    runtime_relative = "versions/$previous/AnxinBoard.Runtime"
    manifest_relative = "versions/$previous/runtime-manifest.json"
    updated_at = [DateTimeOffset]::UtcNow.ToString('o')
}
$tempState = Join-Path $installRoot ('.install-state-' + [Guid]::NewGuid().ToString('N') + '.json')
try {
    $newState | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $tempState -Encoding utf8
    $tempItem = Assert-PlainPath -Path $tempState
    if ($tempItem.PSIsContainer) { throw 'Temporary rollback state is not a file.' }
    Move-Item -LiteralPath $tempState -Destination $statePath -Force
    $writtenState = Assert-PlainPath -Path $statePath
    if ($writtenState.PSIsContainer) { throw 'Written rollback state is not a file.' }
} finally {
    if (Test-Path -LiteralPath $tempState) { [System.IO.File]::Delete($tempState) }
}

Write-Host 'ANXIN_ROLLBACK=SUCCESS'
Write-Host "CURRENT_PAYLOAD_DIGEST=$previous"
