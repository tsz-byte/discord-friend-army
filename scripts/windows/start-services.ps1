param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot/../..").Path,
    [int]$BackendPort = 8000
)

$ErrorActionPreference = "Stop"

$backendCmd = "cd '$RepoRoot/backend'; python -m uvicorn app.main:app --host 0.0.0.0 --port $BackendPort"
$frontendCmd = "cd '$RepoRoot/frontend'; npm run dev -- --host 0.0.0.0"

Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd
Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCmd

Write-Host "[DFA] Backend and frontend processes started in new PowerShell windows."
