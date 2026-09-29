[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputRoot,
    [Parameter(Mandatory = $true)]
    [string]$SourceCommit
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$backendDir = Join-Path $repoRoot 'apps\backend'
$frontendDir = Join-Path $repoRoot 'apps\frontend'
$requirements = Join-Path $backendDir 'requirements.txt'
$buildRequirements = Join-Path $PSScriptRoot 'build-requirements.txt'
$entrypoint = Join-Path $backendDir 'product_entry.py'
$diagnosticsScript = Join-Path $PSScriptRoot 'collect-diagnostics.ps1'

if ([string]::IsNullOrWhiteSpace($OutputRoot)) { throw 'OutputRoot is required.' }
if ($SourceCommit -notmatch '^[0-9a-fA-F]{40}$') { throw 'SourceCommit must be an exact 40-character Git commit SHA.' }
$SourceCommit = $SourceCommit.ToLowerInvariant()
$git = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $git) { $git = Get-Command git -ErrorAction SilentlyContinue }
if (-not $git) { throw 'git is required on the disposable build machine to verify exact Product provenance.' }
$repoHead = (& $git.Source -C $repoRoot rev-parse HEAD).Trim().ToLowerInvariant()
if ($LASTEXITCODE -ne 0 -or $repoHead -notmatch '^[0-9a-f]{40}$') { throw 'Unable to resolve exact Product repository HEAD.' }
if ($repoHead -cne $SourceCommit) {
    throw "SourceCommit must equal exact Product repository HEAD: source=$SourceCommit repo=$repoHead"
}

if (-not [System.IO.Path]::IsPathRooted($OutputRoot)) {
    $OutputRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $OutputRoot))
}
if (Test-Path -LiteralPath $OutputRoot) {
    throw "OutputRoot must not already exist: $OutputRoot"
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python 3.11+ is required on the disposable build machine.' }
$node = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $node) { $node = Get-Command node -ErrorAction SilentlyContinue }
if (-not $node) { throw 'Node.js 20+ is required on the disposable build machine.' }
$npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npm) { $npm = Get-Command npm -ErrorAction SilentlyContinue }
if (-not $npm) { throw 'npm is required on the disposable build machine.' }

$pythonVersion = (& $python.Source -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
if ([version]$pythonVersion -lt [version]'3.11') { throw "Python 3.11+ required; found $pythonVersion" }
$nodeVersion = ((& $node.Source --version).Trim() -replace '^[vV]', '')
if ([version]$nodeVersion -lt [version]'20.0') { throw "Node.js 20+ required; found $nodeVersion" }

New-Item -ItemType Directory -Path $OutputRoot | Out-Null
$venvDir = Join-Path $OutputRoot 'build-venv'
$workDir = Join-Path $OutputRoot 'pyinstaller-work'
$specDir = Join-Path $OutputRoot 'pyinstaller-spec'
$payloadDir = Join-Path $OutputRoot 'payload'

Write-Host '[1/6] Building immutable frontend assets...'
& $npm.Source ci --prefix $frontendDir
if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
& $npm.Source run build --prefix $frontendDir
if ($LASTEXITCODE -ne 0) { throw "frontend build failed with exit code $LASTEXITCODE" }
$frontendDist = Join-Path $frontendDir 'dist'
if (-not (Test-Path (Join-Path $frontendDist 'index.html') -PathType Leaf)) {
    throw 'frontend dist/index.html was not produced'
}

Write-Host '[2/6] Creating isolated packaging environment...'
& $python.Source -m venv $venvDir
if ($LASTEXITCODE -ne 0) { throw "build venv creation failed with exit code $LASTEXITCODE" }
$buildPython = Join-Path $venvDir 'Scripts\python.exe'
& $buildPython -m pip install --disable-pip-version-check -r $requirements -r $buildRequirements
if ($LASTEXITCODE -ne 0) { throw "packaging dependencies failed with exit code $LASTEXITCODE" }

# Stage the exact browser outside site-packages. Its short explicit PyInstaller
# destination stays below Windows MAX_PATH even under versions/<64-char digest>.
# Neither build nor runtime depends on a user browser or global browser cache.
$browserInstallRoot = Join-Path $OutputRoot 'build-browser'
$previousBrowserPath = [Environment]::GetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', 'Process')
try {
    $env:PLAYWRIGHT_BROWSERS_PATH = $browserInstallRoot
    & $buildPython -m playwright install --only-shell chromium
    if ($LASTEXITCODE -ne 0) { throw "bundled screenshot browser installation failed with exit code $LASTEXITCODE" }
}
finally {
    [Environment]::SetEnvironmentVariable('PLAYWRIGHT_BROWSERS_PATH', $previousBrowserPath, 'Process')
}
$playwrightPackage = (& $buildPython -c "import pathlib, playwright; print(pathlib.Path(playwright.__file__).resolve().parent / 'driver' / 'package')").Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to locate the pinned Playwright package.' }
$sourceBrowserManifest = Get-Content -LiteralPath (Join-Path $playwrightPackage 'browsers.json') -Raw -Encoding utf8 | ConvertFrom-Json
$sourceHeadless = @($sourceBrowserManifest.browsers | Where-Object { $_.name -eq 'chromium-headless-shell' })
if ($sourceHeadless.Count -ne 1 -or [string]$sourceHeadless[0].revision -notmatch '^\d+$') {
    throw 'pinned screenshot browser revision is invalid'
}
$sourceRevision = [string]$sourceHeadless[0].revision
$headlessFiles = Join-Path $browserInstallRoot ('chromium_headless_shell-' + $sourceRevision + '\chrome-headless-shell-win64')
if (-not (Test-Path -LiteralPath (Join-Path $headlessFiles 'chrome-headless-shell.exe') -PathType Leaf)) {
    throw 'downloaded screenshot browser executable is missing'
}

Write-Host '[3/6] Building standalone onedir runtime...'
New-Item -ItemType Directory -Path $workDir, $specDir, $payloadDir | Out-Null
& $buildPython -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --name 'AnxinBoard.Runtime' `
    --collect-all playwright `
    --add-data ($headlessFiles + ';report-browser/' + $sourceRevision) `
    --paths $backendDir `
    --distpath $payloadDir `
    --workpath $workDir `
    --specpath $specDir `
    $entrypoint
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

$runtimeDir = Join-Path $payloadDir 'AnxinBoard.Runtime'
$runtimeExe = Join-Path $runtimeDir 'AnxinBoard.Runtime.exe'
if (-not (Test-Path $runtimeExe -PathType Leaf)) { throw 'packaged runtime executable is missing' }
$browserPackage = Join-Path $runtimeDir '_internal\playwright\driver\package'
$browserManifest = Get-Content -LiteralPath (Join-Path $browserPackage 'browsers.json') -Raw -Encoding utf8 | ConvertFrom-Json
$headlessRevision = @($browserManifest.browsers | Where-Object { $_.name -eq 'chromium-headless-shell' })
if ($headlessRevision.Count -ne 1 -or [string]$headlessRevision[0].revision -notmatch '^\d+$') {
    throw 'packaged screenshot browser revision is invalid'
}
$headlessRoot = Join-Path $runtimeDir ('_internal\report-browser\' + [string]$headlessRevision[0].revision)
$headlessExe = Join-Path $headlessRoot 'chrome-headless-shell.exe'
if (-not (Test-Path -LiteralPath $headlessExe -PathType Leaf)) { throw 'packaged screenshot browser executable is missing' }
if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir '_internal\playwright\driver\node.exe') -PathType Leaf)) {
    throw 'packaged screenshot browser driver is missing'
}

Write-Host '[4/6] Attaching built UI beside runtime...'
$uiDir = Join-Path $runtimeDir 'ui'
New-Item -ItemType Directory -Path $uiDir | Out-Null
Copy-Item -Path (Join-Path $frontendDist '*') -Destination $uiDir -Recurse

Write-Host '[5/6] Attaching bounded diagnostics tool...'
if (-not (Test-Path $diagnosticsScript -PathType Leaf)) { throw 'diagnostics script is missing' }
$toolsDir = Join-Path $runtimeDir 'tools'
New-Item -ItemType Directory -Path $toolsDir | Out-Null
Copy-Item -LiteralPath $diagnosticsScript -Destination (Join-Path $toolsDir 'collect-diagnostics.ps1')

Write-Host '[6/6] Writing integrity/provenance manifest...'
$sourceCommit = $SourceCommit
$files = @(
    Get-ChildItem -LiteralPath $runtimeDir -Recurse -File |
        Sort-Object { $_.FullName.Substring($runtimeDir.Length + 1).Replace('\', '/') } |
        ForEach-Object {
            $relative = $_.FullName.Substring($runtimeDir.Length + 1).Replace('\', '/')
            [ordered]@{
                path = $relative
                size = [int64]$_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
)
$manifest = [ordered]@{
    schema_version = 'anxin_windows_runtime_payload_v1'
    source_commit = $sourceCommit
    architecture = $env:PROCESSOR_ARCHITECTURE
    runtime = 'AnxinBoard.Runtime.exe'
    frontend = 'ui/index.html'
    diagnostics = 'tools/collect-diagnostics.ps1'
    files = $files
}
$manifestPath = Join-Path $payloadDir 'runtime-manifest.json'
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding utf8

Write-Host "WINDOWS_RUNTIME_SOURCE_COMMIT=$sourceCommit"
Write-Host "WINDOWS_RUNTIME_PAYLOAD=$runtimeDir"
Write-Host "WINDOWS_RUNTIME_MANIFEST=$manifestPath"
