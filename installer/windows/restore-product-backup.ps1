[CmdletBinding()]
param(
    [string]$BackupPath,
    [switch]$ConfirmRestore,
    [string]$TestInstallRoot,
    [string]$TestDataRoot,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$authorityEnv = 'ANXINBOARD_OFFLINE_RESTORE_AUTHORITY'
$authorityValue = 'launcher-lock-held-v1'
$launchLock = $null
$oldAuthority = [Environment]::GetEnvironmentVariable($authorityEnv, 'Process')
$oldDbPath = [Environment]::GetEnvironmentVariable('ANXINBOARD_DB_PATH', 'Process')

function Resolve-LocalPlainFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not [System.IO.Path]::IsPathRooted($Path)) { throw 'Backup path must be absolute.' }
    $full = [System.IO.Path]::GetFullPath($Path)
    if ($full -match '^(\\\\|//)') { throw 'Backup path must be local, not UNC/network.' }
    if ([System.IO.Path]::GetExtension($full).ToLowerInvariant() -ne '.zip') { throw 'Backup path must be a .zip file.' }
    $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    if ($item.PSIsContainer) { throw 'Backup path must be a file.' }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Backup path must not be a reparse point.' }
    $parent = $item.Directory
    while ($null -ne $parent) {
        $ancestor = Get-Item -LiteralPath $parent.FullName -Force -ErrorAction Stop
        if (($ancestor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Backup path ancestor must not be a reparse point.' }
        $parent = $parent.Parent
    }
    return $full
}

function Resolve-DataRoot {
    if (-not [string]::IsNullOrWhiteSpace($TestDataRoot)) {
        if ($env:ANXINBOARD_INSTALLER_TEST_MODE -ne '1') { throw 'TestDataRoot is available only in explicit installer test mode.' }
        if (-not [System.IO.Path]::IsPathRooted($TestDataRoot)) { throw 'TestDataRoot must be absolute.' }
        $root = [System.IO.Path]::GetFullPath($TestDataRoot)
    } else {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is unavailable.' }
        $root = Join-Path $env:LOCALAPPDATA 'AnxinBoard'
    }
    if ($root -match '^(\\\\|//)') { throw 'Data root must be local.' }
    return $root
}

function Resolve-InstallRoot {
    if (-not [string]::IsNullOrWhiteSpace($TestInstallRoot)) {
        if ($env:ANXINBOARD_INSTALLER_TEST_MODE -ne '1') { throw 'TestInstallRoot is available only in explicit installer test mode.' }
        if (-not [System.IO.Path]::IsPathRooted($TestInstallRoot)) { throw 'TestInstallRoot must be absolute.' }
        return [System.IO.Path]::GetFullPath($TestInstallRoot)
    }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is unavailable.' }
    return (Join-Path $env:LOCALAPPDATA 'Programs\AnxinBoard')
}

function Select-BackupFile {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = '选择安心看板备份包'
    $dialog.Filter = '安心看板备份 (*.zip)|*.zip'
    $dialog.CheckFileExists = $true
    $dialog.Multiselect = $false
    try {
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { return $null }
        return [string]$dialog.FileName
    } finally {
        $dialog.Dispose()
    }
}

function Confirm-UserRestore {
    Add-Type -AssemblyName System.Windows.Forms
    $text = "恢复会替换当前安心看板项目数据库。软件会先检查备份完整性，并拒绝任何可能让系统忘记已发生模型/邮件发送记录的旧备份。`r`n`r`n确定继续吗？"
    $answer = [System.Windows.Forms.MessageBox]::Show(
        $text,
        '安心看板 - 恢复备份',
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Warning,
        [System.Windows.Forms.MessageBoxDefaultButton]::Button2
    )
    return $answer -eq [System.Windows.Forms.DialogResult]::Yes
}

try {
    if ([string]::IsNullOrWhiteSpace($BackupPath)) {
        if ($NonInteractive) { throw 'BackupPath is required in non-interactive mode.' }
        $BackupPath = Select-BackupFile
        if ([string]::IsNullOrWhiteSpace($BackupPath)) { exit 2 }
    }
    $BackupPath = Resolve-LocalPlainFile -Path $BackupPath

    if (-not $ConfirmRestore) {
        if ($NonInteractive) { throw 'Explicit -ConfirmRestore is required in non-interactive mode.' }
        if (-not (Confirm-UserRestore)) { exit 2 }
    }

    $dataRoot = Resolve-DataRoot
    $installRoot = Resolve-InstallRoot
    New-Item -ItemType Directory -Path $dataRoot -Force | Out-Null
    $dataRootItem = Get-Item -LiteralPath $dataRoot -Force
    if (-not $dataRootItem.PSIsContainer -or ($dataRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Product data root is not a plain local directory.'
    }

    $launchLockFile = Join-Path $dataRoot 'secure-launch.lock'
    try {
        $launchLock = [System.IO.File]::Open(
            $launchLockFile,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        throw '安心看板正在启动或另一个恢复流程正在进行；本次恢复未执行。'
    }

    # Enumerating the parent catches files, directories, symlinks and dangling reparse
    # entries named local-run.json.  A normal restore never guesses whether a stale-looking
    # ownership record is safe to ignore.
    $runtimeOwner = @(Get-ChildItem -LiteralPath $dataRoot -Force -ErrorAction Stop | Where-Object { $_.Name -ieq 'local-run.json' })
    if ($runtimeOwner.Count -ne 0) {
        throw '安心看板仍处于运行状态或无法证明已完全关闭。请先正常关闭软件，再执行恢复。'
    }

    $resolver = Join-Path $PSScriptRoot 'resolve-installed-runtime.ps1'
    if (-not (Test-Path -LiteralPath $resolver -PathType Leaf)) { throw 'Installed runtime resolver is missing.' }
    $resolverArgs = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $resolver)
    if (-not [string]::IsNullOrWhiteSpace($TestInstallRoot)) { $resolverArgs += @('-TestInstallRoot', $installRoot) }
    $resolvedOutput = @(& powershell.exe @resolverArgs)
    if ($LASTEXITCODE -ne 0) { throw 'Installed runtime integrity resolution failed.' }
    $resolved = (($resolvedOutput -join "`n").Trim() | ConvertFrom-Json)
    if ($resolved.schema_version -ne 'anxin_installed_runtime_resolution_v1') { throw 'Installed runtime resolver schema mismatch.' }
    if ([System.IO.Path]::IsPathRooted([string]$resolved.runtime_executable_relative)) { throw 'Installed runtime resolver returned an unsafe path.' }
    if ($resolved.launcher_authority -ne 'not_performed' -or $resolved.credential_access -ne 'not_performed') {
        throw 'Installed runtime resolver crossed an unrelated authority boundary.'
    }

    $runtimeExecutable = [System.IO.Path]::GetFullPath((Join-Path $installRoot ([string]$resolved.runtime_executable_relative).Replace('/', '\')))
    $installPrefix = [System.IO.Path]::GetFullPath($installRoot).TrimEnd('\') + '\'
    if (-not $runtimeExecutable.StartsWith($installPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Resolved runtime executable escapes the installed product root.'
    }
    $runtimeItem = Get-Item -LiteralPath $runtimeExecutable -Force -ErrorAction Stop
    if ($runtimeItem.PSIsContainer -or ($runtimeItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Resolved runtime executable is not a plain file.'
    }

    [Environment]::SetEnvironmentVariable($authorityEnv, $authorityValue, 'Process')
    if (-not [string]::IsNullOrWhiteSpace($TestDataRoot)) {
        [Environment]::SetEnvironmentVariable('ANXINBOARD_DB_PATH', (Join-Path $dataRoot 'anxinboard.db'), 'Process')
    }

    & $runtimeExecutable --restore-backup $BackupPath --confirm-restore
    $restoreExit = $LASTEXITCODE
    if ($restoreExit -ne 0) { throw "安心看板拒绝或无法完成恢复（exit $restoreExit）。当前数据库未被不安全覆盖。" }
    Write-Host '安心看板备份恢复完成。下一次正常启动会读取恢复后的项目数据。'
} finally {
    [Environment]::SetEnvironmentVariable($authorityEnv, $oldAuthority, 'Process')
    [Environment]::SetEnvironmentVariable('ANXINBOARD_DB_PATH', $oldDbPath, 'Process')
    if ($null -ne $launchLock) {
        try { $launchLock.Dispose() } catch { }
    }
}

exit 0
