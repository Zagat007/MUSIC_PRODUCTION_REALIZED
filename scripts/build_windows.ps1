# Build Windows executables with PyInstaller (onedir). FFmpeg is NOT bundled (GPL build); the app finds it on
# PATH, via SRM_FFMPEG/SRM_FFPROBE, or in tools\ffmpeg\ next to the repository.
param([ValidateSet('all', 'cli', 'gui')][string]$Target = 'all', [string]$DistPath = 'dist')
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$distRoot = [IO.Path]::GetFullPath((Join-Path $root $DistPath))
if (-not $distRoot.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The output folder must be inside this repository.'
}
if ($Target -in @('all', 'gui')) {
    $runningGui = Get-Process -Name srm-gui -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -eq "$distRoot\srm-gui\srm-gui.exe"
    }
    if ($runningGui) { throw "The GUI is running. Keep its bundle intact; close it or build to a separate folder." }
}
$py = if (Test-Path "$root\.venv\Scripts\python.exe") { "$root\.venv\Scripts\python.exe" } elseif (Test-Path "$root\..\.venv\Scripts\python.exe") { "$root\..\.venv\Scripts\python.exe" } else { "python" }
& $py -c "import sys, PyInstaller; assert (3, 11) <= sys.version_info[:2] < (3, 13), 'Use Python 3.11 or 3.12'"
if ($LASTEXITCODE -ne 0) { throw "Build environment missing. Run setup_windows.ps1 and install the dev dependencies first." }
# Codex's native tools add Poppler/ICU folders to PATH. PyInstaller may bundle that ICU in place
# of the Windows ICU used by Qt, producing a QtCore DLL entry-point failure on startup.
# Only the child builder gets the filtered PATH; the caller's environment stays unchanged.
$builder = "import os,subprocess,sys; env=dict(os.environ); env['PATH']=';'.join(p for p in env.get('PATH','').split(';') if '\\dependencies\\native\\' not in p.lower()); sys.exit(subprocess.call([sys.executable,'-m','PyInstaller',*sys.argv[1:]],env=env))"
if ($Target -in @('all', 'cli')) {
& $py -c $builder --noconfirm --clean --distpath $distRoot --name srm --console --paths $root `
    --collect-submodules srm --exclude-module PySide6 scripts\srm_cli_entry.py
if ($LASTEXITCODE -ne 0) { throw "CLI build failed" }
}
if ($Target -in @('all', 'gui')) {
& $py -c $builder --noconfirm --clean --distpath $distRoot --name srm-gui --windowed --paths $root `
    --collect-submodules srm scripts\srm_gui_entry.py
if ($LASTEXITCODE -ne 0) { throw "GUI build failed" }
}
Copy-Item LICENSE-THIRD-PARTY.md $distRoot -Force
$sourceHashes = [ordered]@{}
Get-ChildItem -LiteralPath "$root\srm" -Filter *.py -Recurse | Sort-Object FullName | ForEach-Object {
    $relative = $_.FullName.Substring($root.Length + 1).Replace('\', '/')
    $sourceHashes[$relative] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
}
$buildRecord = [ordered]@{
    built_at_utc = [DateTime]::UtcNow.ToString('o')
    source_commit = (git rev-parse HEAD)
    source_files_sha256 = $sourceHashes
    executables_sha256 = [ordered]@{
        'srm/srm.exe' = (Get-FileHash -LiteralPath "$distRoot\srm\srm.exe" -Algorithm SHA256).Hash
        'srm-gui/srm-gui.exe' = (Get-FileHash -LiteralPath "$distRoot\srm-gui\srm-gui.exe" -Algorithm SHA256).Hash
    }
}
$buildRecord | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath "$distRoot\BUILD_INFO.json" -Encoding UTF8
Write-Host "Built: $distRoot\srm\srm.exe and $distRoot\srm-gui\srm-gui.exe"
