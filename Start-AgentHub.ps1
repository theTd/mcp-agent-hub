$ErrorActionPreference = "Stop"
$HubDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $HubDir
Write-Host "Starting MCP Non-Headless Sub-Agent Hub on http://127.0.0.1:4444 ..." -ForegroundColor Cyan
& "$HubDir\.venv\Scripts\python.exe" "$HubDir\server.py"
