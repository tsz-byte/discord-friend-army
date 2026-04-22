import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path

# Project root is four directory levels above this file:
# backend/app/core/logging.py → core → app → backend → project_root
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'level': record.levelname,
            'name': record.name,
            'message': record.getMessage(),
        }
        if hasattr(record, 'event_type'):
            payload['event_type'] = record.event_type
        if hasattr(record, 'details'):
            payload['details'] = record.details
        return json.dumps(payload)


def configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # --- Console handler (existing behaviour) ---
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(JsonFormatter())

    # --- File handler: all INFO+ messages → app.log ---
    app_log_path = _PROJECT_ROOT / 'app.log'
    app_file_handler = logging.handlers.RotatingFileHandler(
        str(app_log_path),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding='utf-8',
    )
    app_file_handler.setLevel(logging.INFO)
    app_file_handler.setFormatter(JsonFormatter())

    # --- File handler: ERROR+ messages → errors.txt ---
    error_log_path = _PROJECT_ROOT / 'errors.txt'
    error_file_handler = logging.handlers.RotatingFileHandler(
        str(error_log_path),
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=3,
        encoding='utf-8',
    )
    error_file_handler.setLevel(logging.ERROR)
    error_file_handler.setFormatter(JsonFormatter())

    root.handlers = [stream_handler, app_file_handler, error_file_handler]

    # --- Dedicated captcha diagnostics log ---
    logs_dir = _PROJECT_ROOT / 'logs'
    logs_dir.mkdir(parents=True, exist_ok=True)
    captcha_log_handler = logging.handlers.RotatingFileHandler(
        str(logs_dir / 'captcha_solutions.log'),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8',
    )
    captcha_log_handler.setLevel(logging.INFO)
    captcha_log_handler.setFormatter(JsonFormatter())

    captcha_logger = logging.getLogger('discord_research.captcha_solutions')
    captcha_logger.setLevel(logging.INFO)
    captcha_logger.handlers = [captcha_log_handler]
    captcha_logger.propagate = False
