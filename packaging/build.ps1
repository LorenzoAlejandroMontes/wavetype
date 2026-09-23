# Build Wavetype for Windows: PyInstaller onedir -> secret check -> Inno Setup installer.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1              # dist\Wavetype-Setup.exe
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -WithLocal   # + offline engine (bigger)
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -ZipOnly     # no Inno Setup: a zip
#
# Needs Python 3.11+ on PATH. Inno Setup 6 for the installer: winget install JRSoftware.InnoSetup
# Outputs go to dist\ (gitignored). The build stops if anything that looks like a user file or a
# Groq key ends up in it.
param(
    [string]$Version = "0.1.1",
    [switch]$WithLocal,
    [switch]$ZipOnly
)
# native tools write progress to stderr: with "Stop", PowerShell 5.1 would treat that as a failure.
# Every step checks $LASTEXITCODE and throws instead.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# 1. build environment (.venv, gitignored)
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "== creating .venv"
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed" }
}
Write-Host "== installing requirements + PyInstaller"
& $Py -m pip install -q --upgrade pip
& $Py -m pip install -q -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# 2. icon + installer artwork from docs\img\logo-square.png (same bytes every time)
& $Py packaging\make_icon.py

# 3. PyInstaller onedir
Write-Host "== PyInstaller"
if ($WithLocal) { $env:WAVETYPE_WITH_LOCAL = "1" } else { Remove-Item Env:WAVETYPE_WITH_LOCAL -ErrorAction SilentlyContinue }
& $Py -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\wavetype.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
$App = Join-Path $Root "dist\Wavetype"

# 4. secret check: no user files, no Groq key anywhere in the build
Write-Host "== secret check"
$banned = @("groq_key.txt", "vocab.txt", "card_style.txt", "skin.txt", "wavetype.log",
            "wavetype_history.txt", "last_rec.wav", "groq_usage.json")
$bad = Get-ChildItem $App -Recurse -File | Where-Object { $banned -contains $_.Name }
$bad += Get-ChildItem $App -Recurse -Directory | Where-Object { @("recordings", "models") -contains $_.Name }
if ($bad) { $bad | ForEach-Object { Write-Host "  found: $($_.FullName)" }; throw "user files in the build" }
$keyHits = Get-ChildItem $App -Recurse -File | Select-String -Pattern "gsk_[A-Za-z0-9]{20,}" -List
if ($keyHits) { $keyHits | ForEach-Object { Write-Host "  key pattern in: $($_.Path)" }; throw "Groq key pattern in the build" }
$mb = [math]::Round(((Get-ChildItem $App -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "   ok: no user files, no gsk_ key pattern. dist\Wavetype = $mb MB"

# 5. installer (Inno Setup), or a zip when it is not available
$Iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($ZipOnly -or -not $Iscc) {
    if (-not $ZipOnly) { Write-Host "   Inno Setup not found: making a zip instead" }
    $Zip = Join-Path $Root "dist\Wavetype-$Version-win64.zip"
    if (Test-Path $Zip) { Remove-Item $Zip }
    Compress-Archive -Path $App -DestinationPath $Zip
    Write-Host "== done: $Zip"
    exit 0
}
Write-Host "== Inno Setup"
& $Iscc "/DAppVersion=$Version" packaging\wavetype.iss
if ($LASTEXITCODE -ne 0) { throw "ISCC failed" }
$Setup = Join-Path $Root "dist\Wavetype-Setup.exe"   # no version in the name: releases/latest/download/Wavetype-Setup.exe
$smb = [math]::Round((Get-Item $Setup).Length / 1MB, 1)
Write-Host "== done: $Setup ($smb MB)"
