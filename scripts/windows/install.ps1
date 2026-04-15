param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot/../..").Path,
    [string]$PythonCmd = "python",
    [string]$NodeCmd = "npm"
)

$ErrorActionPreference = "Stop"
Write-Host "[DFA] Starting Windows setup from $RepoRoot"

Set-Location "$RepoRoot/backend"
& $PythonCmd -m pip install -r requirements.txt
Copy-Item -Path ".env.example" -Destination ".env" -ErrorAction SilentlyContinue

Set-Location "$RepoRoot/frontend"
& $NodeCmd install
Copy-Item -Path ".env.example" -Destination ".env" -ErrorAction SilentlyContinue

Set-Location "$RepoRoot"
Write-Host "[DFA] Setup complete. Run scripts/windows/validate.ps1 next."
