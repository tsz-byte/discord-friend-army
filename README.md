# Discord Friend Army

A multi-account Discord bot mimic system with AI-powered conversation generation, proxy rotation, cross-server conversation transfer, and a modern web dashboard.

## Features

- **Multi-Account Management** — Load and manage multiple Discord account tokens simultaneously
- **Intelligent Proxy Rotation** — Automatic proxy rotation with health checks and failover
- **Cross-Server Conversation Sync** — Capture and transfer conversations between Discord servers
- **AI Chat Integration** — OpenRouter API with Grok-4.1-fast for intelligent response generation
- **Modern Dashboard** — Dark-themed tabbed UI with real-time stats, account/proxy management, and activity monitoring
- **Single-Command Startup** — One `python start.py` command bootstraps everything
- **File-Based Credentials** — Simple `t.txt` (tokens), `p.txt` (proxies), `api_key.conf` (API config) files

## Quick Start

### Step 1: Prepare credentials

**`t.txt`** — Discord tokens, one per line:
```
MTQ4MzU0NTA5MjU4ODMxMDY2OQ.GSITFd.bVNznSTbUb_sskxAVZMZnIeAfqhGuSI-ld8x_8
MTE5NjY2MDkwNjkwNjYyODE2OA.GssFyI.jZ9kiJ1uBwtKjn6VYM3GAeTiBPsA8R_kq92XhE
```

**`p.txt`** — Proxies, one per line (`host:port:username:password`):
```
pr-eu.proxies.fo:13337:szent9mfyq-session-ek8c0-ttl-5:jmr6tcfwso
proxy-server.com:8080:username:password
```

**`api_key.conf`** — OpenRouter + captcha solver configuration:
```
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxxxxxxxxxxxxxxx
AI_MODEL=x-ai/grok-4.1-fast
MAX_TOKENS=4096
TEMPERATURE=0.7
RESPONSE_TIMEOUT=30
CAPTCHA_SERVICE=anysolver
CAPTCHA_TASK_TYPE=PopularCaptchaTokenProxyLess
CAPTCHA_SSL_VERIFY=true
CAPTCHA_API_KEY=your-primary-captcha-key
CAPTCHA_2CAPTCHA_API_KEY=
ANTICAPTCHA_API_KEY=
DEATHBYCAPTCHA_API_KEY=
ANYSOLVER_API_KEY=your-anysolver-key
```

### Step 2: Install dependencies

```bash
cd backend && pip install -r requirements.txt
cd ../frontend && npm install
```

### Step 3: Run

```bash
python start.py
```

### Step 4: Open dashboard

Navigate to **http://localhost:8000** — the dashboard loads with all accounts and proxies validated.

## Architecture

```
discord-friend-army/
├── start.py             # Unified startup entry point
├── t.txt                # Discord tokens (one per line)
├── p.txt                # Proxy list (host:port:user:pass per line)
├── api_key.conf         # OpenRouter + AnySolver API configuration
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI application
│   │   ├── core/config.py   # Settings (Pydantic)
│   │   ├── api/routes.py    # All API endpoints
│   │   ├── models/          # SQLAlchemy models
│   │   ├── schemas/         # Pydantic request/response schemas
│   │   ├── services/
│   │   │   ├── ai_chat.py           # OpenRouter Grok-4.1-fast integration
│   │   │   ├── file_loader.py       # t.txt / p.txt / api_key.conf parser
│   │   │   ├── token_manager.py     # Token rotation + health checks
│   │   │   ├── discord_client.py    # Discord API client
│   │   │   ├── captcha_solver.py    # Multi-service captcha solver (AnySolver/2Captcha/AntiCaptcha/DeathByCaptcha)
│   │   │   ├── replication_engine.py# Conversation mirroring engine
│   │   │   ├── pattern_analyzer.py  # Message pattern capture
│   │   │   └── ...
│   │   └── db/session.py    # SQLite/PostgreSQL with fallback
│   └── tests/
├── frontend/
│   └── src/
│       ├── App.tsx          # Tabbed dashboard (8 panels)
│       └── App.css          # Dark theme styles
└── docker-compose.yml
```

## Dashboard Panels

| Tab | Description |
|-----|-------------|
| **Overview** | Active accounts, proxy stats, AI metrics, activity heatmap, sentiment trends |
| **Accounts** | Load tokens from t.txt, add/remove accounts, health checks, enable/disable |
| **Proxies** | Load proxies from p.txt, health indicators, success rates |
| **Servers** | Source/target server config, invite link copy, channel mappings |
| **AI Config** | OpenRouter API key, model settings, test AI chat |
| **Sync** | Cross-server sync controls, pattern capture, replication runs |
| **Activity** | Real-time log feed with search/filter |
| **Settings** | Config snapshot, credential file reloading |

## API Endpoints

### Credential Management
- `POST /api/v1/accounts/load-file` — Load tokens from `t.txt`
- `POST /api/v1/proxies/load-file` — Load proxies from `p.txt`
- `POST /api/v1/config/load-file` — Load settings from `api_key.conf`

### Account & Proxy Operations
- `POST /api/v1/replication/tokens` — Add individual token
- `POST /api/v1/replication/tokens/{id}/health-check` — Check token health
- `POST /api/v1/replication/tokens/rotate` — Rotate to next token
- `GET /api/v1/proxies/health` — Proxy health metrics
- `GET /api/v1/dashboard/stats` — Dashboard statistics

### AI Integration
- `POST /api/v1/ai/chat` — Send AI chat message via Grok-4.1-fast

### Conversation Sync
- `POST /api/v1/replication/servers` — Add server connection
- `POST /api/v1/replication/channel-mappings` — Map source → target channels
- `POST /api/v1/replication/patterns/capture` — Capture message patterns
- `POST /api/v1/replication/control/start` — Start replication session
- `GET /api/v1/replication/control/conversations` — View mirrored conversations
- `GET /api/v1/replication/status` — System status overview

### Analytics
- `GET /api/v1/analytics/overview?guild_id=...`

## Captcha Configuration Notes

- `CAPTCHA_SERVICE` accepts `anysolver`, `2captcha`, `anticaptcha`, `deathbycaptcha` (or comma-separated priority order).
- `CAPTCHA_FALLBACK_SERVICES` configures automatic fallback order if the primary service fails.
- `CAPTCHA_TASK_TYPE` is fully configurable for hCaptcha/reCaptcha task variants required by your provider.
- `CAPTCHA_SSL_VERIFY=false` bypasses TLS verification (only for troubleshooting weak provider certificates).
- `CAPTCHA_CA_BUNDLE_PATH` can be used to trust a custom CA bundle instead of disabling verification.
- `GET /api/v1/analytics/sentiment-trend?guild_id=...`
- `GET /api/v1/analytics/activity-heatmap?guild_id=...`

### Settings
- `PATCH /api/v1/settings/update` — Update runtime settings

## Windows Quick Setup

```powershell
.\scripts\windows\install.ps1
.\scripts\windows\start-services.ps1
```

Or use the menu launcher:
```bat
run_application.bat
```

## Token Format

- **Simple format**: One Discord token per line in `t.txt`
- **Legacy format**: `email:password:token` (auto-detected, token extracted)

## Proxy Format

- `host:port:username:password` — one per line in `p.txt`
- Example: `pr-eu.proxies.fo:13337:szent9mfyq-session-ek8c0-ttl-5:jmr6tcfwso`
