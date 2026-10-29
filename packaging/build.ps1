# Builds Trimmer.exe and the installer. Run from anywhere in PowerShell:
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# Needs Python 3.10+. The installer step needs Inno Setup 6
# (winget install JRSoftware.InnoSetup); without it you still get dist\Trimmer.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".build-venv")) { python -m venv .build-venv }
$py = Join-Path $root ".build-venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r requirements.txt pyinstaller

$version = (& $py packaging\make_version_info.py).Trim()
Write-Host "Building Trimmer $version"
& $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\trimmer.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc "/DAppVersion=$version" packaging\installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    Write-Host "Installer: dist\Trimmer-$version-setup.exe"
} else {
    Write-Host "Inno Setup not found, so no installer was made. The app is in dist\Trimmer."
}
