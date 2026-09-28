[CmdletBinding()]
param(
    [switch]$ConfirmProgramRemoval,
    [string]$TestInstallRoot
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-NoReparseExistingAncestorChain {
    param([string]$Path, [string]$Label)
    $current = [System.IO.DirectoryInfo]::new([System.IO.Path]::GetFullPath($Path))
    while ($null -ne $current) {
        if (Test-Path -LiteralPath $current.FullName) {
            $item = Get-Item -LiteralPath $current.FullName -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label path ancestor contains a reparse point; refusing removal."
            }
        }
        $current = $current.Parent
    }
}

if (-not $ConfirmProgramRemoval) {
    throw 'Program removal requires explicit -ConfirmProgramRemoval. User data is preserved.'
}

if (-not [string]::IsNullOrWhiteSpace($TestInstallRoot)) {
    if ($env:ANXINBOARD_INSTALLER_TEST_MODE -ne '1') { throw 'TestInstallRoot is available only in explicit installer test mode.' }
    if (-not [System.IO.Path]::IsPathRooted($TestInstallRoot)) { throw 'TestInstallRoot must be absolute.' }
    $installRoot = [System.IO.Path]::GetFullPath($TestInstallRoot)
} else {
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is unavailable.' }
    $installRoot = Join-Path $env:LOCALAPPDATA 'Programs\AnxinBoard'
}
if ($installRoot -match '^(\\\\|//)') { throw 'Install root must be a local path.' }

if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is unavailable.' }
$dataRoot = Join-Path $env:LOCALAPPDATA 'AnxinBoard'
Assert-NoReparseExistingAncestorChain -Path $installRoot -Label 'Install root'
Assert-NoReparseExistingAncestorChain -Path $dataRoot -Label 'Product data root'

# Fail closed on any durable runtime-ownership object, not only a regular file.
# Test-Path -PathType Leaf follows links and can miss a dangling symlink/reparse object.
# Enumerating the already-approved immediate data-root children lets us reject a file,
# directory, junction/symlink, or dangling link occupying the exact ownership name.
if (Test-Path -LiteralPath $dataRoot) {
    $dataRootItem = Get-Item -LiteralPath $dataRoot -Force
    if (($dataRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Product data root is a reparse point; runtime ownership cannot be safely classified.'
    }
    if (-not $dataRootItem.PSIsContainer) {
        throw 'Product data root is not a directory; runtime ownership cannot be safely classified.'
    }
    try {
        $runtimeOwnership = @(
            Get-ChildItem -LiteralPath $dataRoot -Force |
                Where-Object { $_.Name -ieq 'local-run.json' }
        )
    } catch {
        throw 'Product data root cannot be enumerated; runtime ownership cannot be safely classified.'
    }
    if ($runtimeOwnership.Count -gt 0) {
        throw 'A runtime ownership object exists. Stop AnxinBoard through its launcher before uninstalling.'
    }
}

if (-not (Test-Path -LiteralPath $installRoot)) {
    Write-Host 'ANXIN_UNINSTALL=ALREADY_ABSENT'
    Write-Host 'USER_DATA=PRESERVED'
    exit 0
}

$rootItem = Get-Item -LiteralPath $installRoot -Force
if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'Install root is a reparse point; refusing removal.'
}
foreach ($item in Get-ChildItem -LiteralPath $installRoot -Recurse -Force) {
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Install tree contains a reparse point; refusing removal.'
    }
}

# Formal release remains gated by P6/Taskbook evidence. This action removes only the
# fixed product-program root; %LOCALAPPDATA%\AnxinBoard user data is never deleted here.
[System.IO.Directory]::Delete($installRoot, $true)
if (Test-Path -LiteralPath $installRoot) { throw 'Program root still exists after uninstall.' }

Write-Host 'ANXIN_UNINSTALL=SUCCESS'
Write-Host 'USER_DATA=PRESERVED'
