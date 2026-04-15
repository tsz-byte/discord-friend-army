#!/usr/bin/env python3
"""Discord Friend Army - Unified Startup Script

Loads t.txt (tokens), p.txt (proxies), and api_key.conf (OpenRouter config),
initializes the database, and starts the FastAPI server.
"""

import os
import sys
import logging
import traceback

# Ensure backend package is importable
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(ROOT_DIR, 'backend')
sys.path.insert(0, BACKEND_DIR)

logging.basicConfig(
    level=logging.DEBUG,
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
    try:
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
    except Exception as exc:
        logger.error(f'Failed to load api_key.conf: {exc}')
        logger.debug(traceback.format_exc())


def load_credentials() -> tuple[int, int]:
    """Load tokens and proxies from files. Returns (tokens_loaded, proxies_loaded)."""
    from app.db.session import SessionLocal, Base, engine
    from app.services.file_loader import FileLoaderService

    tokens_loaded = 0
    proxies_loaded = 0

    try:
        Base.metadata.create_all(bind=engine)
    except Exception as exc:
        logger.error(f'Database initialization failed: {exc}')
        logger.debug(traceback.format_exc())
        return tokens_loaded, proxies_loaded

    db = SessionLocal()
    loader = FileLoaderService()

    try:
        t_path = os.path.join(ROOT_DIR, 't.txt')
        if os.path.isfile(t_path):
            try:
                loaded, errors = loader.load_tokens_file(db, t_path)
                tokens_loaded = loaded
                logger.info(f'  [t.txt] Loaded {loaded} token(s)')
                for err in errors:
                    logger.error(f'  [t.txt] ERROR: {err}')
            except Exception as exc:
                logger.error(f'  [t.txt] Failed to load tokens: {exc}')
                logger.debug(traceback.format_exc())
        else:
            logger.warning('  [t.txt] Token file not found at %s', t_path)

        p_path = os.path.join(ROOT_DIR, 'p.txt')
        if os.path.isfile(p_path):
            try:
                loaded, errors = loader.load_proxies_file(db, p_path)
                proxies_loaded = loaded
                logger.info(f'  [p.txt] Loaded {loaded} {"proxy" if loaded == 1 else "proxies"}')
                for err in errors:
                    logger.error(f'  [p.txt] ERROR: {err}')
            except Exception as exc:
                logger.error(f'  [p.txt] Failed to load proxies: {exc}')
                logger.debug(traceback.format_exc())
        else:
            logger.warning('  [p.txt] Proxy file not found at %s', p_path)
    finally:
        db.close()

    return tokens_loaded, proxies_loaded


def show_server_connections() -> None:
    """Display configured source/target server connections and invite links."""
    try:
        from app.db.session import SessionLocal
        from app.models.research import ServerConnection

        db = SessionLocal()
        try:
            connections = db.query(ServerConnection).filter(ServerConnection.enabled.is_(True)).all()
            if not connections:
                logger.info('  No server connections configured yet.')
                logger.info('  Use the API to add source/target servers with invite links.')
                return
            for conn in connections:
                invite = conn.invite_link or '(no invite link set)'
                logger.info(f'  [{conn.role.upper()}] {conn.guild_name} (ID: {conn.guild_id}) — invite: {invite} — status: {conn.joined_status}')
        finally:
            db.close()
    except Exception as exc:
        logger.error(f'  Failed to read server connections: {exc}')
        logger.debug(traceback.format_exc())


def main() -> None:
    print_banner()

    errors_found = False

    logger.info('Step 1: Loading API configuration...')
    load_api_config()

    logger.info('Step 2: Loading credentials (tokens + proxies)...')
    tokens_loaded, proxies_loaded = load_credentials()

    logger.info('Step 3: Checking server connections...')
    show_server_connections()

    logger.info('Step 4: Starting web server...')
    print()
    print('-' * 60)
    print(f'  Tokens loaded:  {tokens_loaded}')
    print(f'  Proxies loaded: {proxies_loaded}')
    print(f'  Dashboard:      http://localhost:8000')
    print(f'  API docs:       http://localhost:8000/docs')
    print('-' * 60)
    print()

    if tokens_loaded == 0:
        logger.warning('No tokens loaded. Add tokens to t.txt (one per line) and restart.')
    if proxies_loaded == 0:
        logger.warning('No proxies loaded. Add proxies to p.txt and restart (optional).')

    import uvicorn
    uvicorn.run(
        'app.main:app',
        host='0.0.0.0',
        port=8000,
        reload=False,
        log_level='info',
    )


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logger.error(f'FATAL: Startup failed — {exc}')
        logger.error(traceback.format_exc())
        sys.exit(1)
