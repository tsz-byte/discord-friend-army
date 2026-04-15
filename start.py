#!/usr/bin/env python3
"""Discord Friend Army - Unified Startup Script

Loads t.txt (tokens), p.txt (proxies), and api_key.conf (OpenRouter config),
initializes the database, and starts the FastAPI server.
"""

import os
import sys
import logging

# Ensure backend package is importable
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(ROOT_DIR, 'backend')
sys.path.insert(0, BACKEND_DIR)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger('discord_friend_army')


def print_banner() -> None:
    print()
    print('=' * 60)
    print('   Discord Friend Army')
    print('   Multi-Account Bot Mimic System')
    print('=' * 60)
    print()


def load_api_config() -> None:
    conf_path = os.path.join(ROOT_DIR, 'api_key.conf')
    if not os.path.isfile(conf_path):
        logger.warning('api_key.conf not found — using defaults')
        return
    with open(conf_path, 'r', encoding='utf-8') as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip()
                env_map = {
                    'OPENROUTER_API_KEY': 'DFA_OPENROUTER_API_KEY',
                    'AI_MODEL': 'DFA_OPENROUTER_MODEL',
                    'MAX_TOKENS': 'DFA_OPENROUTER_MAX_TOKENS',
                    'TEMPERATURE': 'DFA_OPENROUTER_TEMPERATURE',
                    'RESPONSE_TIMEOUT': 'DFA_OPENROUTER_RESPONSE_TIMEOUT',
                }
                env_key = env_map.get(key)
                if env_key and value:
                    os.environ.setdefault(env_key, value)
                    logger.info(f'  [api_key.conf] {key} loaded')
    logger.info('API configuration loaded from api_key.conf')


def load_credentials() -> None:
    from app.db.session import SessionLocal, Base, engine
    from app.services.file_loader import FileLoaderService

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    loader = FileLoaderService()

    try:
        t_path = os.path.join(ROOT_DIR, 't.txt')
        if os.path.isfile(t_path):
            loaded, errors = loader.load_tokens_file(db, t_path)
            logger.info(f'  [t.txt] Loaded {loaded} token(s)')
            for err in errors:
                logger.warning(f'  [t.txt] {err}')
        else:
            logger.warning('  [t.txt] Token file not found')

        p_path = os.path.join(ROOT_DIR, 'p.txt')
        if os.path.isfile(p_path):
            loaded, errors = loader.load_proxies_file(db, p_path)
            logger.info(f'  [p.txt] Loaded {loaded} {"proxy" if loaded == 1 else "proxies"}')
            for err in errors:
                logger.warning(f'  [p.txt] {err}')
        else:
            logger.warning('  [p.txt] Proxy file not found')
    finally:
        db.close()


def main() -> None:
    print_banner()

    logger.info('Step 1: Loading API configuration...')
    load_api_config()

    logger.info('Step 2: Loading credentials (tokens + proxies)...')
    load_credentials()

    logger.info('Step 3: Starting web server...')
    logger.info('  Dashboard: http://localhost:8000')
    logger.info('  API docs:  http://localhost:8000/docs')
    print()

    import uvicorn
    uvicorn.run(
        'app.main:app',
        host='0.0.0.0',
        port=8000,
        reload=False,
        log_level='info',
    )


if __name__ == '__main__':
    main()
