# Creates a local virtual environment and installs dependencies.
# Run once:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "Creating virtual environment in .venv ..." -ForegroundColor Cyan
python -m venv .venv
& "$root\.venv\Scripts\python.exe" -m pip install --upgrade pip
& "$root\.venv\Scripts\python.exe" -m pip install -r requirements.txt

Write-Host ""
Write-Host "Done. Next steps:" -ForegroundColor Green
Write-Host "  1. Copy .env.example to .env and fill in the keys."
Write-Host "  2. (Optional) install ffmpeg:  winget install Gyan.FFmpeg"
Write-Host "  3. Test:  powershell -ExecutionPolicy Bypass -File scripts\run_brief.ps1 --local --skip-audio"
