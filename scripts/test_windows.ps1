# Run the full test suite and keep a log with the exit code.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "..\.venv\Scripts\python.exe" }
$env:QT_QPA_PLATFORM = "offscreen"
New-Item -ItemType Directory -Force tests\qa | Out-Null
& $py -m pytest -q -p no:cacheprovider --durations=10 *>&1 | Tee-Object -FilePath tests\qa\pytest_log.txt
$code = $LASTEXITCODE
"exit code: $code" | Tee-Object -FilePath tests\qa\pytest_log.txt -Append
exit $code
