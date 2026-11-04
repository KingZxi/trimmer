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

# Licences next to Trimmer.exe (the installer and the portable zip take them from here).
$app = Join-Path $root "dist\Trimmer"
Copy-Item LICENSE, THIRD-PARTY-NOTICES.md, CHANGELOG.md $app -Force
New-Item -ItemType Directory -Force (Join-Path $app "LICENSES") | Out-Null
Copy-Item LICENSES\* (Join-Path $app "LICENSES") -Force
$pyHome = (& $py -c "import sys; print(sys.base_prefix)").Trim()
Copy-Item (Join-Path $pyHome "LICENSE.txt") (Join-Path $app "LICENSES\Python-LICENSE.txt") -Force

# Portable zip: unzip anywhere and run Trimmer.exe.
$zip = Join-Path $root "dist\Trimmer-$version-portable.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path $app -DestinationPath $zip
Write-Host "Portable: dist\Trimmer-$version-portable.zip"

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
