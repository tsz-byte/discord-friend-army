param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot/../..").Path,
    [string]$PythonCmd = "python",
    [string]$NodeCmd = "npm"
)

$ErrorActionPreference = "Stop"
Write-Host "[DFA] Running backend tests"
Set-Location "$RepoRoot/backend"
& $PythonCmd -m pytest

Write-Host "[DFA] Running frontend lint/build"
Set-Location "$RepoRoot/frontend"
& $NodeCmd run lint
& $NodeCmd run build

Set-Location "$RepoRoot"
Write-Host "[DFA] Validation complete"
