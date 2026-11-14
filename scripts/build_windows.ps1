# Build Windows executables with PyInstaller (onedir). FFmpeg is NOT bundled (GPL build); the app finds it on
# PATH, via SRM_FFMPEG/SRM_FFPROBE, or in tools\ffmpeg\ next to the repository.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = if (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } else { "python" }
& $py -m pip install "pyinstaller>=6" | Out-Null
& $py -m PyInstaller --noconfirm --clean --name srm --console --paths $root `
    --collect-submodules srm --exclude-module PySide6 scripts\srm_cli_entry.py
if ($LASTEXITCODE -ne 0) { throw "CLI build failed" }
& $py -m PyInstaller --noconfirm --clean --name srm-gui --windowed --paths $root `
    --collect-submodules srm scripts\srm_gui_entry.py
if ($LASTEXITCODE -ne 0) { throw "GUI build failed" }
Copy-Item LICENSE-THIRD-PARTY.md dist\ -Force
Write-Host "Built: dist\srm\srm.exe and dist\srm-gui\srm-gui.exe"
