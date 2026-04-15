import logging

logger = logging.getLogger('discord_research.activity')


def log_event(event_type: str, details: dict) -> None:
    logger.info('activity_event', extra={'event_type': event_type, 'details': details})
