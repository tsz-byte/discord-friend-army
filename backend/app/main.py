from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import Base, engine

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


@app.on_event('startup')
def startup_event() -> None:
    Base.metadata.create_all(bind=engine)


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
