from fastapi import FastAPI

from app.api.routes import router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import Base, engine

settings = get_settings()
configure_logging()
app = FastAPI(title=settings.app_name, version=settings.app_version)


@app.on_event('startup')
def startup_event() -> None:
    Base.metadata.create_all(bind=engine)


@app.get('/health')
def health() -> dict:
    return {'status': 'ok', 'environment': settings.app_env, 'version': settings.app_version}


app.include_router(router)
