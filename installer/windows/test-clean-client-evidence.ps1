[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('windows10', 'windows11')]
    [string]$ExpectedClient,
    [Parameter(Mandatory = $true)]
    [string]$CandidateZip,
    [Parameter(Mandatory = $true)]
    [string]$CandidateSha256,
    [Parameter(Mandatory = $true)]
    [string]$EvidenceRoot,
    [switch]$AllowDefaultUserInstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($env:ANXINBOARD_DISPOSABLE_VM_EVIDENCE -ne '1') {
    throw 'Clean-client evidence requires ANXINBOARD_DISPOSABLE_VM_EVIDENCE=1.'
}
if (-not $AllowDefaultUserInstall) {
    throw 'Clean-client evidence requires explicit -AllowDefaultUserInstall.'
}
if ($CandidateSha256 -notmatch '^[0-9a-fA-F]{64}$') { throw 'Candidate SHA-256 is invalid.' }
if (-not [System.IO.Path]::IsPathRooted($CandidateZip)) { throw 'CandidateZip must be absolute.' }
if (-not [System.IO.Path]::IsPathRooted($EvidenceRoot)) { throw 'EvidenceRoot must be absolute.' }
$CandidateZip = [System.IO.Path]::GetFullPath($CandidateZip)
$EvidenceRoot = [System.IO.Path]::GetFullPath($EvidenceRoot)
if ($CandidateZip -match '^(\\\\|//)' -or $EvidenceRoot -match '^(\\\\|//)') { throw 'Evidence inputs must be local paths.' }
if (-not (Test-Path -LiteralPath $CandidateZip -PathType Leaf)) { throw 'Candidate ZIP is missing.' }
$candidateItem = Get-Item -LiteralPath $CandidateZip -Force
if (($candidateItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Candidate ZIP must be a plain file.' }

$actualCandidateSha = (Get-FileHash -LiteralPath $CandidateZip -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualCandidateSha -ne $CandidateSha256.ToLowerInvariant()) { throw 'Candidate ZIP digest mismatch.' }

$os = Get-CimInstance -ClassName Win32_OperatingSystem
$computer = Get-CimInstance -ClassName Win32_ComputerSystem
if ([int]$os.ProductType -ne 1) { throw 'Clean-client evidence requires Windows workstation ProductType=1, not Windows Server.' }
if (-not ([string]$os.Version).StartsWith('10.0.')) { throw 'Unsupported Windows client version.' }
$build = 0
if (-not [int]::TryParse([string]$os.BuildNumber, [ref]$build)) { throw 'Windows build number is invalid.' }
$actualClient = if ($build -ge 22000) { 'windows11' } else { 'windows10' }
if ($actualClient -ne $ExpectedClient) { throw "Expected $ExpectedClient but detected $actualClient build $build." }

$virtualIdentity = "{0} {1}" -f ([string]$computer.Manufacturer), ([string]$computer.Model)
if ($virtualIdentity -notmatch '(?i)(virtual|vmware|virtualbox|kvm|qemu|hyper-v|parallels|ec2|google compute)') {
    throw 'Clean-client evidence refuses a machine that is not recognizably virtual/disposable.'
}
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is unavailable.' }
$programRoot = Join-Path $env:LOCALAPPDATA 'Programs\AnxinBoard'
$dataRoot = Join-Path $env:LOCALAPPDATA 'AnxinBoard'
if (Test-Path -LiteralPath $programRoot) { throw 'Clean-client evidence requires AnxinBoard program root to be absent initially.' }
if (Test-Path -LiteralPath $dataRoot) { throw 'Clean-client evidence requires AnxinBoard data root to be absent initially.' }

New-Item -ItemType Directory -Path $EvidenceRoot -Force | Out-Null
$evidenceItem = Get-Item -LiteralPath $EvidenceRoot -Force
if (($evidenceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'EvidenceRoot must be a plain directory.' }

$workRoot = Join-Path $env:TEMP ('anxin-clean-client-' + [Guid]::NewGuid().ToString('N'))
$extractRoot = Join-Path $workRoot 'candidate'
New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null

function Assert-DeliveryBundle {
    param([string]$Root)
    $manifestPath = Join-Path $Root 'delivery-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Delivery manifest is missing.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    if ($manifest.schema_version -ne 'anxin_windows_delivery_candidate_v1') { throw 'Delivery manifest schema mismatch.' }
    if ($manifest.formal_release -ne $false) { throw 'This pre-release clean-client harness only accepts non-formal candidates.' }
    $expected = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in @($manifest.files)) {
        $relative = [string]$entry.path
        if ([string]::IsNullOrWhiteSpace($relative) -or $relative.Contains('..') -or [System.IO.Path]::IsPathRooted($relative)) {
            throw 'Delivery manifest path is invalid.'
        }
        if (-not $expected.Add($relative)) { throw 'Delivery manifest contains duplicate member identity.' }
        if ([string]$entry.sha256 -notmatch '^[0-9a-f]{64}$') { throw 'Delivery member digest is invalid.' }
        $candidate = Join-Path $Root $relative.Replace('/', '\')
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { throw "Delivery member missing: $relative" }
        $item = Get-Item -LiteralPath $candidate -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Delivery member is a reparse point.' }
        if ([int64]$item.Length -ne [int64]$entry.size) { throw "Delivery member size mismatch: $relative" }
        $digest = (Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($digest -ne [string]$entry.sha256) { throw "Delivery member digest mismatch: $relative" }
    }
    $actual = @(
        Get-ChildItem -LiteralPath $Root -Recurse -File -Force |
            Where-Object { $_.FullName -ne $manifestPath } |
            ForEach-Object { $_.FullName.Substring($Root.Length + 1).Replace('\', '/') }
    )
    if ($actual.Count -ne $expected.Count) { throw 'Delivery bundle member set mismatch.' }
    foreach ($relative in $actual) {
        if (-not $expected.Contains($relative)) { throw 'Delivery bundle contains an unmanifested member.' }
    }
    return $manifest
}

function Invoke-InstalledRuntimeResolver {
    param([string]$Tool)
    $output = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Tool)
    if ($LASTEXITCODE -ne 0) { throw 'Installed runtime resolver failed.' }
    $text = ($output -join "`n").Trim()
    if ([string]::IsNullOrWhiteSpace($text)) { throw 'Installed runtime resolver returned no evidence.' }
    $resolved = $text | ConvertFrom-Json
    if ($resolved.schema_version -ne 'anxin_installed_runtime_resolution_v1') { throw 'Installed runtime resolver schema mismatch.' }
    if ([string]$resolved.payload_digest -notmatch '^[0-9a-f]{64}$') { throw 'Installed runtime resolver payload digest is invalid.' }
    if ([System.IO.Path]::IsPathRooted([string]$resolved.runtime_executable_relative)) { throw 'Installed runtime resolver leaked an absolute executable path.' }
    if ($resolved.launcher_authority -ne 'not_performed' -or $resolved.credential_access -ne 'not_performed') {
        throw 'Installed runtime resolver crossed launcher or credential authority.'
    }
    return $resolved
}

function Resolve-InstalledDiagnosticsTool {
    param([string]$InstalledPayload)
    $manifestPath = Join-Path $InstalledPayload 'runtime-manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Installed runtime manifest is missing for diagnostics.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    if ($manifest.schema_version -ne 'anxin_windows_runtime_payload_v1') { throw 'Installed diagnostics manifest schema mismatch.' }
    $relative = [string]$manifest.diagnostics
    if ([string]::IsNullOrWhiteSpace($relative) -or [System.IO.Path]::IsPathRooted($relative) -or $relative.Contains('..')) {
        throw 'Installed diagnostics binding is invalid.'
    }
    $tool = Join-Path (Join-Path $InstalledPayload 'AnxinBoard.Runtime') $relative.Replace('/', '\')
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw 'Installed diagnostics tool is missing.' }
    $toolItem = Get-Item -LiteralPath $tool -Force
    if (($toolItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Installed diagnostics tool is a reparse point.' }
    return $tool
}

$result = [ordered]@{
    schema_version = 'anxin_clean_windows_client_evidence_v1'
    expected_client = $ExpectedClient
    detected_client = $actualClient
    os_version = [string]$os.Version
    os_build = $build
    product_type = [int]$os.ProductType
    virtual_machine_attested = $true
    candidate_sha256 = $actualCandidateSha
    bundle_integrity = 'pending'
    install = 'pending'
    runtime_resolver = 'pending'
    runtime_smoke = 'pending'
    idempotent_reinstall = 'pending'
    synthetic_upgrade = 'pending'
    rollback = 'pending'
    diagnostics = 'pending'
    uninstall = 'pending'
    user_data_preserved = 'pending'
    launcher_130_integration = 'not_exercised'
    credential_secret_access = 'not_performed'
}

try {
    Expand-Archive -LiteralPath $CandidateZip -DestinationPath $extractRoot
    $manifest = Assert-DeliveryBundle -Root $extractRoot
    $result.bundle_integrity = 'passed'

    $payloadRoot = Join-Path $extractRoot 'payload'
    $installTool = Join-Path $extractRoot ([string]$manifest.install_tool).Replace('/', '\')
    $rollbackTool = Join-Path $extractRoot ([string]$manifest.rollback_tool).Replace('/', '\')
    $uninstallTool = Join-Path $extractRoot ([string]$manifest.uninstall_tool).Replace('/', '\')
    $resolverTool = Join-Path $extractRoot ([string]$manifest.runtime_resolver_tool).Replace('/', '\')
    foreach ($tool in @($installTool, $rollbackTool, $uninstallTool, $resolverTool)) {
        if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw 'Required candidate lifecycle tool is missing.' }
    }

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installTool -PayloadRoot $payloadRoot
    if ($LASTEXITCODE -ne 0) { throw 'Clean-client install failed.' }
    $result.install = 'passed'

    $statePath = Join-Path $programRoot 'install-state.json'
    $state = Get-Content -LiteralPath $statePath -Raw -Encoding utf8 | ConvertFrom-Json
    $firstDigest = [string]$state.current_payload_digest
    if ($firstDigest -notmatch '^[0-9a-f]{64}$') { throw 'Installed state payload digest is invalid.' }
    $resolvedInitial = Invoke-InstalledRuntimeResolver -Tool $resolverTool
    if ([string]$resolvedInitial.payload_digest -ne $firstDigest) { throw 'Installed runtime resolver disagrees with install state.' }
    $result.runtime_resolver = 'passed'

    $installedPayload = Join-Path (Join-Path $programRoot 'versions') $firstDigest
    $testRuntime = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'test-runtime.ps1'
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $testRuntime -PayloadRoot $installedPayload
    if ($LASTEXITCODE -ne 0) { throw 'Clean-client packaged runtime smoke failed.' }
    $result.runtime_smoke = 'passed'

    # Reinstalling the exact same payload must be idempotent and retain one active identity.
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installTool -PayloadRoot $payloadRoot
    if ($LASTEXITCODE -ne 0) { throw 'Clean-client idempotent reinstall failed.' }
    $sameState = Get-Content -LiteralPath $statePath -Raw -Encoding utf8 | ConvertFrom-Json
    if ([string]$sameState.current_payload_digest -ne $firstDigest) { throw 'Idempotent reinstall changed active payload identity.' }
    $result.idempotent_reinstall = 'passed'

    # Exercise the actual side-by-side upgrade and rollback machinery without inventing
    # a second release artifact: change only manifest provenance in a disposable copy.
    $upgradePayload = Join-Path $workRoot 'synthetic-upgrade-payload'
    New-Item -ItemType Directory -Path $upgradePayload | Out-Null
    Copy-Item -Path (Join-Path $payloadRoot '*') -Destination $upgradePayload -Recurse
    $upgradeManifestPath = Join-Path $upgradePayload 'runtime-manifest.json'
    $upgradeManifest = Get-Content -LiteralPath $upgradeManifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    $upgradeManifest.source_commit = ('2' * 40)
    $upgradeManifest | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $upgradeManifestPath -Encoding utf8

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installTool -PayloadRoot $upgradePayload
    if ($LASTEXITCODE -ne 0) { throw 'Clean-client synthetic upgrade failed.' }
    $upgradeState = Get-Content -LiteralPath $statePath -Raw -Encoding utf8 | ConvertFrom-Json
    $upgradeDigest = [string]$upgradeState.current_payload_digest
    if ($upgradeDigest -notmatch '^[0-9a-f]{64}$' -or $upgradeDigest -eq $firstDigest) { throw 'Synthetic upgrade did not create a new payload identity.' }
    if ([string]$upgradeState.previous_payload_digest -ne $firstDigest) { throw 'Synthetic upgrade did not preserve rollback identity.' }
    $resolvedUpgrade = Invoke-InstalledRuntimeResolver -Tool $resolverTool
    if ([string]$resolvedUpgrade.payload_digest -ne $upgradeDigest) { throw 'Resolver did not follow upgraded active payload.' }
    $result.synthetic_upgrade = 'passed'

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $rollbackTool
    if ($LASTEXITCODE -ne 0) { throw 'Clean-client rollback failed.' }
    $rollbackState = Get-Content -LiteralPath $statePath -Raw -Encoding utf8 | ConvertFrom-Json
    if ([string]$rollbackState.current_payload_digest -ne $firstDigest) { throw 'Rollback did not restore the original payload identity.' }
    $resolvedRollback = Invoke-InstalledRuntimeResolver -Tool $resolverTool
    if ([string]$resolvedRollback.payload_digest -ne $firstDigest) { throw 'Resolver did not follow rolled-back active payload.' }
    $result.rollback = 'passed'

    # Diagnostics evidence must use the installed, runtime-manifest-bound copy rather
    # than the extraction-tree convenience tool. This proves production-relative
    # manifest discovery against the active rolled-back version.
    $activePayload = Join-Path (Join-Path $programRoot 'versions') $firstDigest
    $installedDiagnosticsTool = Resolve-InstalledDiagnosticsTool -InstalledPayload $activePayload
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installedDiagnosticsTool
    if ($LASTEXITCODE -ne 0) { throw 'Clean-client installed diagnostics failed.' }
    $diagnosticsZip = Get-ChildItem -LiteralPath (Join-Path $dataRoot 'diagnostics') -Filter '*.zip' -File | Select-Object -First 1
    if (-not $diagnosticsZip) { throw 'Clean-client diagnostics bundle was not produced.' }
    $result.diagnostics = 'passed'

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $uninstallTool -ConfirmProgramRemoval
    if ($LASTEXITCODE -ne 0) { throw 'Clean-client uninstall failed.' }
    if (Test-Path -LiteralPath $programRoot) { throw 'Clean-client program root survived uninstall.' }
    $result.uninstall = 'passed'
    if (-not (Test-Path -LiteralPath $dataRoot -PathType Container)) { throw 'Clean-client user data root was not preserved.' }
    $result.user_data_preserved = 'passed'
} finally {
    $resultPath = Join-Path $EvidenceRoot "clean-$ExpectedClient-evidence.json"
    $result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $resultPath -Encoding utf8
    if (Test-Path -LiteralPath $workRoot) { [System.IO.Directory]::Delete($workRoot, $true) }
}

Write-Host "CLEAN_WINDOWS_CLIENT=$ExpectedClient"
Write-Host 'CLEAN_WINDOWS_CLIENT_EVIDENCE=SUCCESS_PRE_LAUNCHER'
Write-Host 'LAUNCHER_130_INTEGRATION=NOT_EXERCISED'
Write-Host 'CREDENTIAL_SECRET_ACCESS=NOT_PERFORMED'
