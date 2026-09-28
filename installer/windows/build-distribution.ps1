[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PayloadRoot,
    [Parameter(Mandatory = $true)]
    [string]$OutputRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$gitRuntimePrerequisite = 'Git for Windows 2.45+ with --no-lazy-fetch capability'

function Assert-PlainPath {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { throw "Required delivery path is missing: $Path" }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Delivery bundle input contains a reparse point.'
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

function Assert-NoReparseRelativeChain {
    param([string]$Root, [string]$Relative)
    $current = [System.IO.Path]::GetFullPath($Root)
    Assert-PlainPath -Path $current | Out-Null
    foreach ($part in $Relative.Replace('/', '\').Split('\')) {
        $current = Join-Path $current $part
        Assert-PlainPath -Path $current | Out-Null
    }
}

function Get-PlainRelativeFiles {
    param([string]$Root)
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $rootItem = Assert-PlainPath -Path $rootFull
    if (-not $rootItem.PSIsContainer) { throw 'Delivery payload root must be a directory.' }
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push($rootFull)
    $files = [System.Collections.Generic.List[object]]::new()
    $identities = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Delivery payload contains a reparse point.'
            }
            if ($item.PSIsContainer) {
                $pending.Push($item.FullName)
                continue
            }
            $relative = $item.FullName.Substring($rootFull.Length + 1).Replace('\', '/')
            if (-not $identities.Add($relative)) { throw 'Delivery payload contains duplicate file identity.' }
            $files.Add([pscustomobject]@{ relative = $relative; item = $item })
        }
    }
    return @($files)
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not [System.IO.Path]::IsPathRooted($PayloadRoot)) {
    $PayloadRoot = [System.IO.Path]::GetFullPath($PayloadRoot)
}
$PayloadRoot = [System.IO.Path]::GetFullPath($PayloadRoot)
if (-not [System.IO.Path]::IsPathRooted($OutputRoot)) {
    $OutputRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputRoot))
}
if ($OutputRoot -match '^(\\\\|//)') { throw 'OutputRoot must be local.' }
Assert-PlainPath -Path $PayloadRoot | Out-Null
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
Assert-PlainPath -Path $OutputRoot | Out-Null

$runtimeManifestPath = Join-Path $PayloadRoot 'runtime-manifest.json'
$runtimeDir = Join-Path $PayloadRoot 'AnxinBoard.Runtime'
$manifestItem = Assert-PlainPath -Path $runtimeManifestPath
$runtimeItem = Assert-PlainPath -Path $runtimeDir
if ($manifestItem.PSIsContainer -or -not $runtimeItem.PSIsContainer) { throw 'Runtime payload shape is invalid.' }

$runtimeManifest = Get-Content -LiteralPath $runtimeManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
if ($runtimeManifest.schema_version -ne 'anxin_windows_runtime_payload_v1') { throw 'Runtime payload schema is unsupported.' }
$runtimeEntries = @($runtimeManifest.files)
if ($runtimeEntries.Count -eq 0) { throw 'Runtime payload manifest has no files.' }
$manifestFiles = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($entry in $runtimeEntries) {
    $relative = [string]$entry.path
    if (-not $manifestFiles.Add($relative)) { throw 'Runtime manifest contains duplicate path identity.' }
    if ([string]$entry.sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Runtime manifest member digest is invalid.' }
    $candidate = Resolve-ManifestFile -RuntimeRoot $runtimeDir -Relative $relative
    Assert-NoReparseRelativeChain -Root $runtimeDir -Relative $relative
    $item = Get-Item -LiteralPath $candidate -Force
    if ($item.PSIsContainer) { throw 'Runtime manifest member is not a file.' }
    if ([int64]$item.Length -ne [int64]$entry.size) { throw "Runtime payload file size mismatch: $relative" }
    $digest = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($digest -ne ([string]$entry.sha256).ToLowerInvariant()) { throw "Runtime payload file digest mismatch: $relative" }
}
foreach ($binding in @('runtime', 'frontend', 'diagnostics')) {
    $relative = [string]$runtimeManifest.$binding
    if ([string]::IsNullOrWhiteSpace($relative) -or -not $manifestFiles.Contains($relative)) {
        throw "Runtime manifest $binding member is not integrity-bound."
    }
}

$runtimeActual = Get-PlainRelativeFiles -Root $runtimeDir
if ($runtimeActual.Count -ne $manifestFiles.Count) { throw 'Runtime payload member set does not match manifest.' }
foreach ($member in $runtimeActual) {
    if (-not $manifestFiles.Contains([string]$member.relative)) { throw 'Runtime payload contains an unmanifested file.' }
}

$payloadFiles = Get-PlainRelativeFiles -Root $PayloadRoot
$expectedPayloadFiles = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
$null = $expectedPayloadFiles.Add('runtime-manifest.json')
foreach ($relative in $manifestFiles) { $null = $expectedPayloadFiles.Add("AnxinBoard.Runtime/$relative") }
if ($payloadFiles.Count -ne $expectedPayloadFiles.Count) { throw 'Payload root contains files outside the runtime manifest contract.' }
foreach ($member in $payloadFiles) {
    if (-not $expectedPayloadFiles.Contains([string]$member.relative)) { throw 'Payload root contains an unmanifested delivery file.' }
}

$payloadManifestDigest = (Get-FileHash -LiteralPath $runtimeManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()

$toolSources = [ordered]@{
    'tools/install-runtime.ps1' = (Join-Path $PSScriptRoot 'install-runtime.ps1')
    'tools/rollback-runtime.ps1' = (Join-Path $PSScriptRoot 'rollback-runtime.ps1')
    'tools/uninstall-product.ps1' = (Join-Path $PSScriptRoot 'uninstall-product.ps1')
    'tools/resolve-installed-runtime.ps1' = (Join-Path $PSScriptRoot 'resolve-installed-runtime.ps1')
    'tools/restore-product-backup.ps1' = (Join-Path $PSScriptRoot 'restore-product-backup.ps1')
}
foreach ($source in $toolSources.Values) {
    $sourceItem = Assert-PlainPath -Path $source
    if ($sourceItem.PSIsContainer) { throw 'Required delivery tool is not a file.' }
}

$members = New-Object System.Collections.Generic.List[object]
foreach ($payloadFile in $payloadFiles) {
    $item = $payloadFile.item
    $relative = [string]$payloadFile.relative
    $members.Add([pscustomobject]@{
        archive_path = "payload/$relative"
        source_path = $item.FullName
        size = [int64]$item.Length
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    })
}
foreach ($pair in $toolSources.GetEnumerator()) {
    $item = Get-Item -LiteralPath $pair.Value -Force
    $members.Add([pscustomobject]@{
        archive_path = [string]$pair.Key
        source_path = $item.FullName
        size = [int64]$item.Length
        sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    })
}
$orderedMembers = @($members | Sort-Object archive_path)
if (-not $orderedMembers.Count) { throw 'Delivery bundle has no members.' }
if (@($orderedMembers.archive_path | Group-Object { $_.ToLowerInvariant() } | Where-Object Count -gt 1).Count) {
    throw 'Delivery bundle contains duplicate archive paths.'
}

$manifest = [ordered]@{
    schema_version = 'anxin_windows_delivery_candidate_v1'
    formal_release = $false
    release_blocker = 'clean Windows evidence and formal signing/release remain pending'
    source_commit = [string]$runtimeManifest.source_commit
    payload_manifest_sha256 = $payloadManifestDigest
    runtime_entry = 'payload/AnxinBoard.Runtime/AnxinBoard.Runtime.exe'
    install_tool = 'tools/install-runtime.ps1'
    rollback_tool = 'tools/rollback-runtime.ps1'
    uninstall_tool = 'tools/uninstall-product.ps1'
    restore_tool = 'tools/restore-product-backup.ps1'
    diagnostics_tool = 'payload/AnxinBoard.Runtime/tools/collect-diagnostics.ps1'
    runtime_resolver_tool = 'tools/resolve-installed-runtime.ps1'
    normal_user_machine_prerequisites = @($gitRuntimePrerequisite)
    reproducibility_scope = 'sorted members + fixed ZIP timestamps + SHA-256 member manifest; byte-identical ZIP requires the same declared .NET compression toolchain'
    files = @($orderedMembers | ForEach-Object {
        [ordered]@{
            path = $_.archive_path
            size = $_.size
            sha256 = $_.sha256
        }
    })
}
$manifestJson = $manifest | ConvertTo-Json -Depth 8 -Compress
$manifestBytes = [System.Text.UTF8Encoding]::new($false).GetBytes($manifestJson)
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $manifestDigest = [System.BitConverter]::ToString($sha.ComputeHash($manifestBytes)).Replace('-', '').ToLowerInvariant()
} finally {
    $sha.Dispose()
}

$zipName = "AnxinBoard-Windows-candidate-$($payloadManifestDigest.Substring(0, 16)).zip"
$zipPath = Join-Path $OutputRoot $zipName
if (Test-Path -LiteralPath $zipPath) { throw "Candidate bundle already exists: $zipPath" }

Add-Type -AssemblyName System.IO.Compression
$fixedTime = [DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
$fileStream = $null
$archive = $null
try {
    $fileStream = [System.IO.File]::Open($zipPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    $archive = [System.IO.Compression.ZipArchive]::new($fileStream, [System.IO.Compression.ZipArchiveMode]::Create, $false)

    $manifestEntry = $archive.CreateEntry('delivery-manifest.json', [System.IO.Compression.CompressionLevel]::Optimal)
    $manifestEntry.LastWriteTime = $fixedTime
    $manifestStream = $manifestEntry.Open()
    try { $manifestStream.Write($manifestBytes, 0, $manifestBytes.Length) } finally { $manifestStream.Dispose() }

    foreach ($member in $orderedMembers) {
        $entry = $archive.CreateEntry([string]$member.archive_path, [System.IO.Compression.CompressionLevel]::Optimal)
        $entry.LastWriteTime = $fixedTime
        $entryStream = $entry.Open()
        $sourceStream = $null
        try {
            $sourceStream = [System.IO.File]::OpenRead([string]$member.source_path)
            $sourceStream.CopyTo($entryStream)
        } finally {
            if ($sourceStream) { $sourceStream.Dispose() }
            $entryStream.Dispose()
        }
    }
} catch {
    if (Test-Path -LiteralPath $zipPath) { [System.IO.File]::Delete($zipPath) }
    throw
} finally {
    if ($archive) { $archive.Dispose() }
    if ($fileStream) { $fileStream.Dispose() }
}

$zipDigest = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$digestPath = "$zipPath.sha256"
"$zipDigest  $zipName" | Set-Content -LiteralPath $digestPath -Encoding ascii

Write-Host 'WINDOWS_DELIVERY_CANDIDATE=BUILT_NOT_RELEASED'
Write-Host "DELIVERY_MANIFEST_SHA256=$manifestDigest"
Write-Host "DELIVERY_ZIP=$zipPath"
Write-Host "DELIVERY_ZIP_SHA256=$zipDigest"
