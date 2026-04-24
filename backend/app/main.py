from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging
from app.db.session import Base, engine
from app.middleware.errors import app_error_handler, generic_error_handler
from app.middleware.request_context import request_logging_middleware

settings = get_settings()
configure_logging()
app = FastAPI(title=settings.app_name, version=settings.app_version)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://127.0.0.1:8007', 'http://localhost:8007', 'http://127.0.0.1:5173', 'http://localhost:5173'],
    allow_credentials=False,
    allow_methods=['*'],
    allow_headers=['*'],
)
app.middleware('http')(request_logging_middleware)
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(Exception, generic_error_handler)


def _run_schema_migrations() -> None:
    """Apply lightweight ADD COLUMN migrations for columns added after initial table creation.

    SQLAlchemy's ``create_all`` only creates missing tables; it never adds columns to
    existing ones.  When a new nullable column is introduced we add it here so that
    existing deployments automatically pick it up on the next startup without requiring
    a full migration framework.
    """
    import logging

    _logger = logging.getLogger('discord_research.db')
    migrations: list[tuple[str, str, str]] = [
        # (table, column, column_definition)
        ('captcha_challenge', 'anysolver_session_id', 'VARCHAR(128)'),
        ('captcha_challenge', 'solution_raw', 'JSON'),
        ('captcha_challenge', 'captcha_context_id', 'TEXT'),
        ('captcha_challenge', 'captcha_context_id_empty', 'BOOLEAN DEFAULT 0'),
        ('captcha_challenge', 'retried_with_empty_context', 'BOOLEAN DEFAULT 0'),
    ]
    from sqlalchemy import inspect, text

    with engine.connect() as conn:
        inspector = inspect(conn)
        for table, column, col_def in migrations:
            try:
                existing_columns = {c['name'] for c in inspector.get_columns(table)}
            except Exception:
                # Table doesn't exist yet — create_all will handle it.
                continue
            if column not in existing_columns:
                _logger.info('schema_migration: adding column %s.%s', table, column)
                conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {col_def}'))
                conn.commit()


@app.on_event('startup')
async def startup_event() -> None:
    Base.metadata.create_all(bind=engine)
    _run_schema_migrations()

    # ------------------------------------------------------------------
    # Log startup configuration summary
    # ------------------------------------------------------------------
    import logging as _logging
    _startup_log = _logging.getLogger('discord_research')
    _startup_log.info(
        '[STARTUP] Discord Friend Army initialising  env=%s  version=%s  runtype=%s',
        settings.app_env,
        settings.app_version,
        settings.runtype,
    )

    # AnySolver API key validation
    _anysolver_key = (settings.anysolver_api_key or '').strip()
    if _anysolver_key:
        # Build a safe preview (first 4 + last 4 chars) — never log the full key.
        _klen = len(_anysolver_key)
        if _klen >= 8:
            _key_tail = _anysolver_key[-4:]
            _key_preview = f'{"*" * 4}...{_key_tail}'
        else:
            _key_preview = '****'
        _startup_log.info('[STARTUP] AnySolver: configured (key_suffix=%s)', _key_preview)
        # Attempt a lightweight connectivity check using /getBalance — a standard
        # AnySolver endpoint that verifies key validity without starting a solve task.
        try:
            import httpx as _httpx
            _base = (settings.anysolver_base_url or 'https://api.anysolver.com').rstrip('/')
            async with _httpx.AsyncClient(timeout=5.0) as _ac:
                _probe = await _ac.post(
                    f'{_base}/getBalance',
                    json={'clientKey': _anysolver_key},
                )
            _startup_log.info('[STARTUP] AnySolver connectivity check status=%s', _probe.status_code)
        except Exception as _exc:
            _startup_log.warning('[STARTUP] AnySolver connectivity check failed: %s', _exc)
    else:
        _startup_log.warning(
            '[STARTUP] AnySolver: NOT configured — set DFA_ANYSOLVER_API_KEY to enable captcha solving'
        )

    # Join logging directory
    from pathlib import Path as _Path
    _log_dir_raw = settings.join_failure_log_dir
    _project_root = _Path(__file__).resolve().parent.parent
    _log_dir = (
        _Path(_log_dir_raw) if _Path(_log_dir_raw).is_absolute()
        else _project_root / _log_dir_raw
    )
    try:
        for _sub in ('join_attempts', 'captcha_challenges', 'gateway_sessions', 'failures'):
            (_log_dir / _sub).mkdir(parents=True, exist_ok=True)
        _startup_log.info('[STARTUP] Join logs directory: %s', _log_dir)
    except Exception as _exc:
        _startup_log.warning('[STARTUP] Could not create join logs directory %s: %s', _log_dir, _exc)

    _startup_log.info(
        '[STARTUP] Discord API: %s  log_all_attempts=%s',
        settings.discord_api_base_url,
        settings.join_log_all_attempts,
    )

    # Auto-start the replication loop if there are tokens + mappings in the DB.
    # We do this in a best-effort fashion — the loop itself logs any errors.
    try:
        from app.db.session import SessionLocal
        from app.models.research import AccountToken, ChannelMapping, AppSetting
        from app.services import auto_replication, realtime_listener
        from app.services.token_manager import TokenManagerService

        db = SessionLocal()
        try:
            active_tokens = db.query(AccountToken).filter(AccountToken.is_active.is_(True)).count()
            enabled_mappings = db.query(ChannelMapping).filter(ChannelMapping.enabled.is_(True)).count()
            manager = TokenManagerService()
            token_rows = db.query(AccountToken).filter(AccountToken.is_active.is_(True)).all()
            for token_row in token_rows:
                await manager.health_check(db, token_row)

            # Read stored interval settings.
            interval_row = db.query(AppSetting).filter(AppSetting.key == 'auto_loop_interval_seconds').first()
            interval = int(interval_row.value) if (interval_row and interval_row.value) else 180

            rt_interval_row = db.query(AppSetting).filter(AppSetting.key == 'realtime_interval_ms').first()
            rt_interval_ms = int(rt_interval_row.value) if (rt_interval_row and rt_interval_row.value) else 1500

            if active_tokens > 0 and enabled_mappings > 0:
                auto_replication.start_loop(interval_seconds=interval)

            # Auto-start the real-time listener whenever enabled channel mappings
            # exist — works for both USERT (user tokens) and BOTT (bot token) modes.
            if enabled_mappings > 0:
                realtime_listener.start_listener(interval_ms=rt_interval_ms)
                import logging as _logging
                _logging.getLogger('discord_research').info(
                    'startup: realtime_listener auto-started (interval_ms=%d, mappings=%d)',
                    rt_interval_ms, enabled_mappings,
                )
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover
        import logging
        logging.getLogger('discord_research').error('startup auto-loop init failed: %s', exc)


@app.get('/health')
def health() -> dict:
    return {'status': 'ok', 'environment': settings.app_env, 'version': settings.app_version}


app.include_router(router)

# ---------------------------------------------------------------------------
# Serve the pre-built React frontend (built by `npm run build` in /frontend).
# If the frontend has not been built yet the API still works fine; the root
# path will return a plain JSON hint instead of the SPA.
# ---------------------------------------------------------------------------
_STATIC_DIR = Path(__file__).parent.parent / 'static'

_assets_dir = _STATIC_DIR / 'assets'
if _assets_dir.is_dir():
    app.mount('/assets', StaticFiles(directory=str(_assets_dir)), name='assets')


@app.get('/', include_in_schema=False)
def serve_root() -> object:
    index = _STATIC_DIR / 'index.html'
    if index.is_file():
        return FileResponse(str(index))
    return {
        'service': 'Discord Friend Army API',
        'docs': '/docs',
        'hint': 'Frontend not built. Run: cd frontend && npm run build',
    }


@app.get('/{full_path:path}', include_in_schema=False)
def serve_spa(full_path: str) -> object:
    # Let API and built-in FastAPI paths pass through naturally.
    if full_path.startswith(('api/', 'docs', 'openapi.json', 'redoc')):
        raise HTTPException(status_code=404)
    # Guard against path-traversal: resolve and ensure it stays inside static dir.
    try:
        candidate = (_STATIC_DIR / full_path).resolve()
        candidate.relative_to(_STATIC_DIR.resolve())
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail='Invalid path')
    if candidate.is_file():
        return FileResponse(str(candidate))
    # SPA fallback — return index.html for client-side routing.
    index = _STATIC_DIR / 'index.html'
    if index.is_file():
        return FileResponse(str(index))
    raise HTTPException(status_code=404, detail='Frontend not built. Run: cd frontend && npm run build')
