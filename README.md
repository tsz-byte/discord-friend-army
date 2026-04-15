# Discord Community Analytics & Research Platform

A privacy-first analytics platform for **authorized Discord communities** focused on academic study of digital communication patterns.

## What this repository includes

- **FastAPI backend** for ingestion, analytics APIs, consent management, and compliance metadata
- **PostgreSQL persistence layer** for anonymized research events and opt-in state
- **Redis-backed caching/rate limiting** with graceful fallback when Redis is unavailable
- **OpenRouter NLP integration** for sentiment and topic modeling (with local fallback heuristics)
- **React + D3 dashboard** for communication flow, sentiment trends, and activity heatmaps
- **Transparent activity logging** with structured JSON log events

## Architecture

- `backend/` — API layer, Discord integration, NLP service, privacy controls
- `frontend/` — researcher/admin dashboard with D3 visualizations
- `docs/` — methodology and compliance guidance

## Compliance and ethics

This project is designed around Discord ToS and academic ethics constraints:

- Official Discord bot API usage only (`Bot` token flows)
- Explicit server-level opt-in (`/api/v1/consent/opt-in`)
- User-level privacy controls (`/api/v1/privacy/user-preferences`)
- Salted SHA-256 anonymization for participant identifiers
- GDPR/CCPA-oriented retention and deletion patterns
- Methodology endpoint for publication transparency (`/api/v1/compliance/methodology`)

## Quick start

### 1) Infrastructure

```bash
docker compose up -d
```

### 2) Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

### 3) Frontend

```bash
cd frontend
cp .env.example .env
npm install
npm run dev
```

## API highlights

- `POST /api/v1/consent/opt-in`
- `POST /api/v1/consent/opt-out?guild_id=...`
- `POST /api/v1/ingest/messages`
- `POST /api/v1/privacy/user-preferences`
- `GET /api/v1/analytics/overview?guild_id=...`
- `GET /api/v1/analytics/sentiment-trend?guild_id=...`
- `GET /api/v1/analytics/activity-heatmap?guild_id=...`
- `GET /api/v1/analytics/interaction-flow?guild_id=...`
- `GET /api/v1/compliance/methodology`

## Research publication support

See `docs/methodology.md` for recommended methodology disclosure and publication workflow.
