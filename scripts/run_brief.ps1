# Runs the brief using the project's virtual environment.
# Any extra args are passed straight through to run_brief.py, e.g.:
#   scripts\run_brief.ps1 --local --skip-audio
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }   # fall back to system Python

& $py "$root\run_brief.py" @args
exit $LASTEXITCODE
