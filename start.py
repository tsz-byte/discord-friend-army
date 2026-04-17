#!/usr/bin/env python3
"""Discord Friend Army - Unified Startup Script

Loads t.txt (tokens), p.txt (proxies), and api_key.conf (OpenRouter + AnySolver config),
seeds the default base/target server connections, builds the frontend, and
starts the FastAPI server on 127.0.0.1:8007.
"""

import os
import shutil
import subprocess
import sys
import logging
import logging.handlers
import traceback

# Ensure backend package is importable
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(ROOT_DIR, 'backend')
FRONTEND_DIR = os.path.join(ROOT_DIR, 'frontend')
sys.path.insert(0, BACKEND_DIR)

HOST = '127.0.0.1'
PORT = 8007

# ---------------------------------------------------------------------------
# Base (copy/mimic) server — conversations are replicated FROM here
# ---------------------------------------------------------------------------
BASE_GUILD_ID = '751274186189701190'
BASE_GUILD_INVITE = 'https://discord.gg/ttzewo'
BASE_CHANNEL_ID = '851143244779487302'

# ---------------------------------------------------------------------------
# Target server — tokens send messages TO here
# ---------------------------------------------------------------------------
TARGET_GUILD_ID = '1425152532807684167'
TARGET_GUILD_INVITE = 'https://discord.gg/asTTvgMe'
TARGET_CHANNEL_ID = '1459350794649342185'

# ---------------------------------------------------------------------------
# Logging — console + rotating file handlers
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger('discord_friend_army')

# Attach a RotatingFileHandler so errors are always persisted on disk.
_error_log_path = os.path.join(ROOT_DIR, 'errors.txt')
_error_file_handler = logging.handlers.RotatingFileHandler(
    _error_log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
)
_error_file_handler.setLevel(logging.ERROR)
_error_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
logging.getLogger().addHandler(_error_file_handler)

_app_log_path = os.path.join(ROOT_DIR, 'app.log')
_app_file_handler = logging.handlers.RotatingFileHandler(
    _app_log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding='utf-8'
)
_app_file_handler.setLevel(logging.INFO)
_app_file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s'))
logging.getLogger().addHandler(_app_file_handler)


def print_banner() -> None:
    print()
    print('=' * 60)
    print('   Discord Friend Army')
    print('   Multi-Account Bot Mimic System')
    print('=' * 60)
    print()


def _ensure_vite_env_var(env_path: str, key: str, value: str) -> None:
    """Write or replace a variable in a Vite .env file.

    Creates the file if it does not exist.  Always ensures ``key=value`` is
    present so the built frontend uses the correct setting regardless of any
    previously existing .env content.
    """
    lines: list[str] = []
    found = False
    if os.path.isfile(env_path):
        with open(env_path, 'r', encoding='utf-8') as fh:
            for line in fh:
                if line.startswith(f'{key}=') or line.startswith(f'{key} ='):
                    lines.append(f'{key}={value}\n')
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f'{key}={value}\n')
    with open(env_path, 'w', encoding='utf-8') as fh:
        fh.writelines(lines)


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
                        'ANYSOLVER_API_KEY': 'DFA_ANYSOLVER_API_KEY',
                        'ANYSOLVER_BASE_URL': 'DFA_ANYSOLVER_BASE_URL',
                        'CAPTCHA_TASK_TYPE': 'DFA_CAPTCHA_TASK_TYPE',
                        'CAPTCHA_SSL_VERIFY': 'DFA_CAPTCHA_SSL_VERIFY',
                        'CAPTCHA_CA_BUNDLE_PATH': 'DFA_CAPTCHA_CA_BUNDLE_PATH',
                        'RUNTYPE': 'DFA_RUNTYPE',
                        'DISCORD_BOT_TOKEN': 'DFA_DISCORD_BOT_TOKEN',
                    }
                    env_key = env_map.get(key)
                    if env_key and value:
                        os.environ.setdefault(env_key, value)
                        logger.info(f'  [api_key.conf] {key} loaded')
        logger.info('API configuration loaded from api_key.conf')
    except Exception as exc:
        logger.error(f'Failed to load api_key.conf: {exc}')
        logger.debug(traceback.format_exc())


def _runtime_mode() -> str:
    value = (os.environ.get('DFA_RUNTYPE', 'USERT') or 'USERT').strip().upper()
    if value not in {'USERT', 'BOTT'}:
        raise ValueError('RUNTYPE in api_key.conf must be USERT or BOTT')
    return value


def validate_runtime_mode() -> str:
    mode = _runtime_mode()
    if mode == 'BOTT' and not os.environ.get('DFA_DISCORD_BOT_TOKEN', '').strip():
        raise ValueError('DISCORD_BOT_TOKEN must be set in api_key.conf when RUNTYPE=BOTT')
    logger.info('Active runtime mode: %s', mode)
    return mode


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
                if proxies_loaded == 0 and not errors:
                    logger.warning(
                        '  [p.txt] File exists but contains no proxy entries. '
                        'Add lines in format: host:port:username:password'
                    )
            except Exception as exc:
                logger.error(f'  [p.txt] Failed to load proxies: {exc}')
                logger.debug(traceback.format_exc())
        else:
            logger.warning('  [p.txt] Proxy file not found at %s', p_path)
    finally:
        db.close()

    return tokens_loaded, proxies_loaded


def seed_default_servers() -> None:
    """Ensure the base and target server connections and channel mapping exist."""
    try:
        from app.db.session import SessionLocal
        from app.models.research import (
            ChannelMapping,
            GuildOptIn,
            ServerConnection,
        )

        db = SessionLocal()
        try:
            # ---- Base (source/mimic) server ----
            opt_in = (
                db.query(GuildOptIn)
                .filter(GuildOptIn.guild_id == BASE_GUILD_ID)
                .first()
            )
            if opt_in is None:
                opt_in = GuildOptIn(
                    guild_id=BASE_GUILD_ID,
                    guild_name='Base Mimic Server',
                    opted_in=True,
                    methodology_version='2026.04',
                )
                db.add(opt_in)
                db.commit()
                logger.info('  [seed] GuildOptIn created for base server %s', BASE_GUILD_ID)

            src = (
                db.query(ServerConnection)
                .filter(
                    ServerConnection.guild_id == BASE_GUILD_ID,
                    ServerConnection.role == 'source',
                )
                .first()
            )
            if src is None:
                src = ServerConnection(
                    guild_id=BASE_GUILD_ID,
                    guild_name='Base Mimic Server',
                    role='source',
                    invite_link=BASE_GUILD_INVITE,
                    enabled=True,
                    joined_status='pending',
                    research_scope='full',
                )
                db.add(src)
                db.commit()
                logger.info(
                    '  [seed] Source server connection created: %s (%s)',
                    BASE_GUILD_ID,
                    BASE_GUILD_INVITE,
                )

            # ---- Target server ----
            tgt = (
                db.query(ServerConnection)
                .filter(
                    ServerConnection.guild_id == TARGET_GUILD_ID,
                    ServerConnection.role == 'target',
                )
                .first()
            )
            if tgt is None:
                tgt = ServerConnection(
                    guild_id=TARGET_GUILD_ID,
                    guild_name='Target Server',
                    role='target',
                    invite_link=TARGET_GUILD_INVITE,
                    enabled=True,
                    joined_status='pending',
                    research_scope='full',
                )
                db.add(tgt)
                db.commit()
                logger.info(
                    '  [seed] Target server connection created: %s (%s)',
                    TARGET_GUILD_ID,
                    TARGET_GUILD_INVITE,
                )

            # ---- Channel mapping ----
            mapping = (
                db.query(ChannelMapping)
                .filter(
                    ChannelMapping.source_guild_id == BASE_GUILD_ID,
                    ChannelMapping.source_channel_id == BASE_CHANNEL_ID,
                    ChannelMapping.target_guild_id == TARGET_GUILD_ID,
                    ChannelMapping.target_channel_id == TARGET_CHANNEL_ID,
                )
                .first()
            )
            if mapping is None:
                mapping = ChannelMapping(
                    source_guild_id=BASE_GUILD_ID,
                    source_channel_id=BASE_CHANNEL_ID,
                    target_guild_id=TARGET_GUILD_ID,
                    target_channel_id=TARGET_CHANNEL_ID,
                    enabled=True,
                    filters={},
                    settings={},
                )
                db.add(mapping)
                db.commit()
                logger.info(
                    '  [seed] Channel mapping created: %s -> %s',
                    BASE_CHANNEL_ID,
                    TARGET_CHANNEL_ID,
                )
        finally:
            db.close()
    except Exception as exc:
        logger.error(f'  [seed] Failed to seed default servers: {exc}')
        logger.debug(traceback.format_exc())


def build_frontend() -> bool:
    """Build the React frontend into backend/static. Returns True on success."""
    npm = shutil.which('npm')
    if npm is None:
        logger.warning(
            '  [frontend] npm not found — skipping frontend build. '
            'Install Node.js and re-run start.py for the dashboard.'
        )
        return False

    env_src = os.path.join(FRONTEND_DIR, '.env.example')
    env_dst = os.path.join(FRONTEND_DIR, '.env')
    if not os.path.isfile(env_dst) and os.path.isfile(env_src):
        shutil.copy(env_src, env_dst)
        logger.info('  [frontend] Copied .env.example → .env')

    # Always enforce the correct API base URL so the built SPA targets the
    # FastAPI backend via a relative path — this fixes 405 errors caused by
    # an incorrectly configured VITE_API_BASE_URL (e.g. missing http:// scheme).
    _ensure_vite_env_var(env_dst, 'VITE_API_BASE_URL', '/api/v1')
    logger.info('  [frontend] Ensured VITE_API_BASE_URL=/api/v1 in .env')

    logger.info('  [frontend] Installing dependencies (npm install)...')
    try:
        subprocess.run(
            [npm, 'install', '--prefer-offline'],
            cwd=FRONTEND_DIR,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.error('  [frontend] npm install failed: %s', exc.stderr.decode(errors='replace')[:400])
        return False

    logger.info('  [frontend] Building frontend (npm run build)...')
    try:
        result = subprocess.run(
            [npm, 'run', 'build'],
            cwd=FRONTEND_DIR,
            check=True,
            capture_output=True,
        )
        logger.info('  [frontend] Build succeeded.')
        if result.stdout:
            for line in result.stdout.decode(errors='replace').splitlines():
                logger.debug('  [frontend] %s', line)
        return True
    except subprocess.CalledProcessError as exc:
        logger.error(
            '  [frontend] npm run build failed:\n%s',
            exc.stderr.decode(errors='replace')[:800],
        )
        return False


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
                logger.info(
                    f'  [{conn.role.upper()}] {conn.guild_name} '
                    f'(ID: {conn.guild_id}) — invite: {invite} — status: {conn.joined_status}'
                )
        finally:
            db.close()
    except Exception as exc:
        logger.error(f'  Failed to read server connections: {exc}')
        logger.debug(traceback.format_exc())


def main() -> None:
    print_banner()

    logger.info('Step 1: Loading API configuration...')
    load_api_config()
    mode = validate_runtime_mode()

    logger.info('Step 2: Loading credentials (tokens + proxies)...')
    tokens_loaded, proxies_loaded = load_credentials()

    logger.info('Step 3: Seeding default server connections...')
    seed_default_servers()

    logger.info('Step 4: Checking server connections...')
    show_server_connections()

    logger.info('Step 5: Building frontend...')
    frontend_built = build_frontend()

    logger.info('Step 6: Starting web server...')
    print()
    print('-' * 60)
    print(f'  Tokens loaded:   {tokens_loaded}')
    print(f'  Proxies loaded:  {proxies_loaded}')
    print(f'  Runtime mode:    {mode}')
    print(f'  Frontend built:  {"yes" if frontend_built else "no (npm missing or build failed)"}')
    print(f'  Dashboard:       http://{HOST}:{PORT}')
    print(f'  API docs:        http://{HOST}:{PORT}/docs')
    print('-' * 60)
    print()

    if mode == 'USERT' and tokens_loaded == 0:
        logger.warning('No tokens loaded. Add tokens to t.txt (one per line) and restart.')
    if proxies_loaded == 0:
        logger.warning(
            'No proxies loaded. '
            'Add proxies to p.txt (format: host:port:username:password) and restart (optional).'
        )

    import uvicorn
    uvicorn.run(
        'app.main:app',
        host=HOST,
        port=PORT,
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
