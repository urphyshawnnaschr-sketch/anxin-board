[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$OutputRoot)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$OutputRoot = [IO.Path]::GetFullPath($OutputRoot)
if ($OutputRoot.StartsWith('\\')) { throw 'Launcher output must be a local path.' }
New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
if ((Get-Item -LiteralPath $OutputRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Launcher output is a reparse point.' }
$exe = Join-Path $OutputRoot '安心看板.exe'
$icon = Join-Path $OutputRoot 'AnxinBoard.ico'
if ((Test-Path -LiteralPath $exe) -or (Test-Path -LiteralPath $icon)) { throw 'Launcher outputs already exist.' }
$csc = Join-Path ([Environment]::GetFolderPath('Windows')) 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc -PathType Leaf)) { throw 'Windows .NET Framework compiler is unavailable.' }
$sourceIcon = Join-Path $PSScriptRoot 'assets\AnxinBoard.ico'
if (-not (Test-Path -LiteralPath $sourceIcon -PathType Leaf)) { throw 'Versioned launcher icon is unavailable.' }
Copy-Item -LiteralPath $sourceIcon -Destination $icon
& $csc /nologo /target:winexe /platform:x64 /optimize+ /r:System.Windows.Forms.dll "/win32icon:$icon" "/out:$exe" (Join-Path $PSScriptRoot 'AnxinBoard.Launcher.cs')
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Native desktop launcher compilation failed.' }
Write-Host 'NATIVE_DESKTOP_LAUNCHER=BUILT'
