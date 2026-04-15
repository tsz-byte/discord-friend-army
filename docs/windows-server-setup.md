# Windows Server Setup Guide

This guide configures the educational Discord replication research stack on Windows Server.

## Prerequisites

- Windows Server 2019+ with administrator access
- Python 3.11+
- Node.js 20+
- PostgreSQL and Redis (or dockerized equivalents)

## Installation Workflow

### Option A: all-in-one starter (recommended)

```bat
run_application.bat
```

This installs dependencies, auto-creates env files, initializes the database, and opens a menu for start/validate/session commands.

### Option B: PowerShell scripts

1. Run installer:

```powershell
.\scripts\windows\install.ps1
```

2. Run setup wizard for environment values:

```powershell
.\scripts\windows\config-wizard.ps1
```

3. Validate backend and frontend:

```powershell
.\scripts\windows\validate.ps1
```

4. Start backend/frontend services:

```powershell
.\scripts\windows\start-services.ps1
```

## Operational Notes

- Keep `DFA_EDUCATIONAL_REPLICATION_ONLY=true` in `backend/.env`.
- Configure source and target servers before creating channel mappings.
- Use queue and status dashboards to monitor failures and retry behavior.
- Replication outputs are best-effort educational simulations, not perfect user impersonation.

## Troubleshooting

- If backend tests fail, rerun `python -m pip install -r backend/requirements.txt`.
- If frontend build fails, rerun `npm install` inside `frontend/`.
- Check `/api/v1/replication/status` for queue backlog and token health before running sessions.
