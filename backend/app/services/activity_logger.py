import logging
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger('discord_research.activity')
_RECENT_EVENTS: deque[dict] = deque(maxlen=400)


def log_event(event_type: str, details: dict) -> None:
    _RECENT_EVENTS.appendleft(
        {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'event_type': event_type,
            'details': details,
        }
    )
    logger.info('activity_event', extra={'event_type': event_type, 'details': details})


def list_recent_activity_events(limit: int = 100) -> list[dict]:
    safe_limit = max(1, min(limit, 400))
    return list(_RECENT_EVENTS)[:safe_limit]
