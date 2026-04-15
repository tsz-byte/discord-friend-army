import logging

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger('discord_research.db')
FALLBACK_SQLITE_DSN = 'sqlite:///./discord_research.db'


def _create_resilient_engine():
    configured_dsn = settings.postgres_dsn
    primary_engine = create_engine(configured_dsn, future=True)
    if configured_dsn.startswith('sqlite'):
        return primary_engine

    try:
        with primary_engine.connect() as connection:
            connection.execute(text('SELECT 1'))
        return primary_engine
    except OperationalError as exc:
        logger.warning(
            'db_connection_fallback',
            extra={
                'event_type': 'db_connection_fallback',
                'details': {
                    'configured_dsn': configured_dsn,
                    'fallback_dsn': FALLBACK_SQLITE_DSN,
                    'reason': str(exc),
                },
            },
        )
        return create_engine(FALLBACK_SQLITE_DSN, future=True)


engine = _create_resilient_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
