import logging

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger('discord_research.db')
FALLBACK_SQLITE_DSN = 'sqlite:///./discord_research.db'
POOL_RECYCLE_SECONDS = 1800  # 30 minutes
SQLITE_BUSY_TIMEOUT_SECONDS = 30


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Configure SQLite for better concurrency on every new connection."""
    cursor = dbapi_connection.cursor()
    # WAL mode allows concurrent reads while a write is in progress.
    cursor.execute('PRAGMA journal_mode=WAL')
    # How long (ms) SQLite waits for a lock before raising OperationalError.
    cursor.execute(f'PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}')
    cursor.close()


def _engine_kwargs(dsn: str) -> dict:
    if dsn.startswith('sqlite'):
        return {
            'future': True,
            'connect_args': {
                'check_same_thread': False,
                'timeout': SQLITE_BUSY_TIMEOUT_SECONDS,
            },
            'poolclass': NullPool,
            'pool_pre_ping': True,
            'pool_recycle': POOL_RECYCLE_SECONDS,
        }
    return {
        'future': True,
        'pool_pre_ping': True,
        'pool_recycle': POOL_RECYCLE_SECONDS,
        'pool_size': 10,
        'max_overflow': 20,
        'pool_timeout': 30,
    }


def _create_resilient_engine():
    configured_dsn = settings.postgres_dsn
    primary_engine = create_engine(configured_dsn, **_engine_kwargs(configured_dsn))
    if configured_dsn.startswith('sqlite'):
        event.listen(primary_engine, 'connect', _set_sqlite_pragmas)
        # SQLite file DBs are created on first write; this keeps startup lightweight and uses configured DSN directly.
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
        fallback_engine = create_engine(FALLBACK_SQLITE_DSN, **_engine_kwargs(FALLBACK_SQLITE_DSN))
        event.listen(fallback_engine, 'connect', _set_sqlite_pragmas)
        return fallback_engine


engine = _create_resilient_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
