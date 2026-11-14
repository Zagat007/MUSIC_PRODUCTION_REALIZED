# Run the full test suite and keep a log with the exit code.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "..\.venv\Scripts\python.exe" }
$env:QT_QPA_PLATFORM = "offscreen"
New-Item -ItemType Directory -Force tests\qa | Out-Null
$out = & $py -m pytest -q -p no:cacheprovider --durations=10 2>&1 | ForEach-Object { "$_" }
$code = $LASTEXITCODE
$out | Out-File -FilePath tests\qa\pytest_log.txt -Encoding utf8
"exit code: $code" | Out-File -FilePath tests\qa\pytest_log.txt -Encoding utf8 -Append
$out
"exit code: $code"
exit $code
