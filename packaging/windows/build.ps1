<#
.SYNOPSIS
    Builds the Windows distribution of Boost.

.DESCRIPTION
    Freezes bin/boost.py into a single boost.exe with PyInstaller and lays out
    a portable folder next to it:

        dist/boost-windows/
            boost.exe          CLI + dashboard launcher
            webui/             dashboard sources, read at startup
            boost-auto.conf    default configuration
            presets.json       canonical mode thresholds
            README.txt         what works on Windows and what does not

    The webui/ files are shipped beside the executable rather than embedded so
    the dashboard stays editable in a release, exactly as it is on Linux.

    A zip of that folder is what CI attaches to a release. There is no MSI:
    Boost on Windows writes to %ProgramData%\Boost and needs no install step.

.PARAMETER OutputDir
    Where to place dist/ and build/. Defaults to the repository root.

.PARAMETER SkipZip
    Build the folder but do not archive it.

.EXAMPLE
    pwsh packaging/windows/build.ps1
#>
[CmdletBinding()]
param(
    [string]$OutputDir,
    [switch]$SkipZip
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path (Join-Path $PSScriptRoot '..') '..')).Path
if (-not $OutputDir) { $OutputDir = $RepoRoot }

$BuildDir   = Join-Path $OutputDir 'build'
$DistDir    = Join-Path $OutputDir 'dist'
$PayloadDir = Join-Path $DistDir 'boost-windows'

Write-Host "[build] Repository : $RepoRoot"
Write-Host "[build] Output     : $OutputDir"

# ── Prerequisites ────────────────────────────────────────────────────────
$python = if (Get-Command python -ErrorAction SilentlyContinue) { 'python' } else { 'python3' }
& $python --version | Write-Host

Write-Host '[build] Ensuring PyInstaller is available...'
& $python -m pip install --disable-pip-version-check --quiet pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'pip install pyinstaller failed' }

# ── Freeze ───────────────────────────────────────────────────────────────
# lib/ is added to the analysis path so boost.py's sibling imports
# (boost_paths, platform_backend, platform_windows) are picked up. boost-web.py
# has a hyphen and is loaded at runtime by path, so it is shipped as data.
Write-Host '[build] Running PyInstaller...'
& $python -m PyInstaller `
    --onefile `
    --name boost `
    --distpath $PayloadDir `
    --workpath $BuildDir `
    --specpath $BuildDir `
    --paths (Join-Path $RepoRoot 'lib') `
    --hidden-import boost_paths `
    --hidden-import platform_backend `
    --hidden-import platform_windows `
    --hidden-import http.server `
    --hidden-import socketserver `
    --hidden-import urllib.parse `
    --hidden-import csv `
    --hidden-import html `
    --console `
    (Join-Path (Join-Path $RepoRoot 'bin') 'boost.py')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }

# ── Payload ──────────────────────────────────────────────────────────────
Write-Host '[build] Staging the portable folder...'

$libOut = Join-Path $PayloadDir 'lib'
New-Item -ItemType Directory -Force -Path $libOut | Out-Null
foreach ($file in 'boost-web.py', 'boost_paths.py', 'platform_backend.py', 'platform_windows.py') {
    Copy-Item (Join-Path (Join-Path $RepoRoot 'lib') $file) -Destination $libOut -Force
}

$webuiOut = Join-Path $libOut 'webui'
New-Item -ItemType Directory -Force -Path $webuiOut | Out-Null
Copy-Item (Join-Path (Join-Path (Join-Path $RepoRoot 'lib') 'webui') '*') -Destination $webuiOut -Force

Copy-Item (Join-Path (Join-Path $RepoRoot 'config') 'boost-auto.conf') -Destination $PayloadDir -Force
Copy-Item (Join-Path (Join-Path $RepoRoot 'config') 'presets.json')    -Destination $PayloadDir -Force
Copy-Item (Join-Path $PSScriptRoot 'README.txt')           -Destination $PayloadDir -Force

# ── Archive ──────────────────────────────────────────────────────────────
if (-not $SkipZip) {
    $zip = Join-Path $DistDir 'boost-windows.zip'
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Write-Host "[build] Creating $zip"
    Compress-Archive -Path (Join-Path $PayloadDir '*') -DestinationPath $zip
}

Write-Host ''
Write-Host '[build] Done.'
Get-ChildItem -Recurse $PayloadDir | Select-Object -ExpandProperty FullName | Write-Host
