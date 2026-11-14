# Create a Python 3.11 virtual environment and install pinned dependencies. Free, no account needed.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = (py -3.11 -c "import sys; print(sys.executable)") 2>$null
if (-not $py) { throw "Python 3.11 not found. Install it from python.org (free) and re-run." }
& $py -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
& .\.venv\Scripts\python.exe -m pip install -e . --no-deps
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
  Write-Warning "FFmpeg not on PATH. Install a free build: winget install Gyan.FFmpeg (or set SRM_FFMPEG/SRM_FFPROBE)."
}
& .\.venv\Scripts\srm.exe --version
