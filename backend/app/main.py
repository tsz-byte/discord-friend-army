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
