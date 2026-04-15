param(
    [string]$RepoRoot = (Resolve-Path "$PSScriptRoot/../..").Path
)

$ErrorActionPreference = "Stop"

$backendEnvPath = "$RepoRoot/backend/.env"
if (!(Test-Path $backendEnvPath)) {
    Copy-Item "$RepoRoot/backend/.env.example" $backendEnvPath
}

$frontendEnvPath = "$RepoRoot/frontend/.env"
if (!(Test-Path $frontendEnvPath)) {
    Copy-Item "$RepoRoot/frontend/.env.example" $frontendEnvPath
}

$apiBase = Read-Host "Enter frontend API base URL (default: http://localhost:8000/api/v1)"
if ([string]::IsNullOrWhiteSpace($apiBase)) { $apiBase = "http://localhost:8000/api/v1" }

Add-Content -Path $frontendEnvPath -Value "`nVITE_API_BASE_URL=$apiBase"
Write-Host "[DFA] Wizard complete. Review backend/.env and frontend/.env before launching services."
