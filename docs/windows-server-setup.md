# Windows Server Setup Guide

This guide configures the Discord replication stack on Windows Server.

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
If PostgreSQL is configured but unreachable, backend startup automatically falls back to local SQLite so service boot does not fail.

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

- Configure source and target servers before creating channel mappings.
- Use queue and status dashboards to monitor failures and retry behavior.
- Replication sends source-channel message history directly to mapped target channels.

## Troubleshooting

- If backend tests fail, rerun `python -m pip install -r backend/requirements.txt`.
- If frontend build fails, rerun `npm install` inside `frontend/`.
- Check `/api/v1/replication/status` for queue backlog and token health before running sessions.
