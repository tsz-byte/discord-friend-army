"""Real-time conversation transfer listener.

Polls source Discord channels continuously and forwards every new message to
the corresponding target channel using round-robin token rotation.

Each enabled :class:`~app.models.research.ChannelMapping` that has
``settings['realtime_enabled'] == True`` is monitored independently.  One
active token is selected per message using a global round-robin index so every
token gets roughly equal sending duty.

Usage::

    from app.services import realtime_listener

    realtime_listener.start_listener(interval_ms=2000)
    ...
    realtime_listener.stop_listener()
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

logger = logging.getLogger('discord_research.realtime_listener')

_active: bool = False
_task: asyncio.Task | None = None
_rotation_index: int = 0
_interval_ms: int = 2000
_stats: dict = {
    'transferred': 0,
    'failed': 0,
    'last_transfer': None,
    'started_at': None,
}


def get_status() -> dict:
    return {
        'active': _active,
        'interval_ms': _interval_ms,
        'task_alive': _task is not None and not _task.done(),
        'stats': dict(_stats),
    }


def start_listener(interval_ms: int = 2000) -> dict:
    """Start (or restart) the real-time listener loop."""
    global _active, _task, _interval_ms, _stats
    _interval_ms = max(500, interval_ms)
    _active = True
    _stats['started_at'] = datetime.now(timezone.utc).isoformat()
    if _task is None or _task.done():
        _task = asyncio.create_task(_listener_loop())
        logger.info('realtime_listener: started (interval_ms=%d)', _interval_ms)
    return get_status()


def stop_listener() -> dict:
    """Stop the real-time listener loop."""
    global _active, _task
    _active = False
    if _task and not _task.done():
        _task.cancel()
        _task = None
        logger.info('realtime_listener: stopped')
    return get_status()


async def _pick_token_round_robin(db) -> object | None:  # returns AccountToken | None
    """Select the next active/healthy token in round-robin order."""
    global _rotation_index
    from app.models.research import AccountToken

    tokens = (
        db.query(AccountToken)
        .filter(
            AccountToken.is_active.is_(True),
            AccountToken.health_status.in_(['healthy', 'unknown']),
        )
        .order_by(AccountToken.id.asc())
        .all()
    )
    if not tokens:
        return None
    token = tokens[_rotation_index % len(tokens)]
    _rotation_index += 1
    token.usage_count = (token.usage_count or 0) + 1
    db.commit()
    return token


async def _listener_loop() -> None:
    """Main polling loop: runs until `_active` is False or task is cancelled."""
    global _active, _stats
    from app.db.session import SessionLocal
    from app.models.research import AccountToken, ChannelMapping, RealtimeTransferEvent
    from app.services.discord_client import DiscordClient
    from app.services.token_manager import TokenManagerService
    from app.services.activity_logger import log_event

    logger.info('realtime_listener: loop started')
    discord_client = DiscordClient()
    token_manager = TokenManagerService()
    # Track last-seen message ID per mapping (keyed by mapping.id)
    last_seen: dict[int, str | None] = {}

    while _active:
        db = SessionLocal()
        try:
            mappings = (
                db.query(ChannelMapping)
                .filter(ChannelMapping.enabled.is_(True))
                .all()
            )

            for mapping in mappings:
                settings = mapping.settings or {}
                if not settings.get('realtime_enabled', False):
                    continue

                # Pick a source token to read the source channel
                source_tokens = (
                    db.query(AccountToken)
                    .filter(
                        AccountToken.is_active.is_(True),
                        AccountToken.health_status.in_(['healthy', 'unknown']),
                    )
                    .order_by(AccountToken.id.asc())
                    .limit(1)
                    .all()
                )
                if not source_tokens:
                    logger.debug('realtime_listener: no active tokens for reading source channel')
                    continue
                source_token_row = source_tokens[0]

                src_proxy: str | None = None
                if source_token_row.proxy_host and source_token_row.proxy_port:
                    src_proxy = token_manager.build_proxy_url(
                        host=source_token_row.proxy_host,
                        port=source_token_row.proxy_port,
                        username=source_token_row.proxy_username or '',
                        password=source_token_row.proxy_password or '',
                    )

                after_id = last_seen.get(mapping.id)
                messages = await discord_client.get_channel_messages(
                    channel_id=mapping.source_channel_id,
                    token=source_token_row.token_value,
                    after=after_id,
                    limit=10,
                    proxy_url=src_proxy,
                )
                if not messages:
                    continue

                # Update last-seen to avoid reprocessing on next poll
                last_seen[mapping.id] = messages[-1]['id']

                for message in messages:
                    content = message.get('content', '').strip()
                    if not content:
                        continue

                    author = message.get('author') or {}
                    author_name = (
                        author.get('global_name') or author.get('username') or 'Unknown'
                    )

                    # Pick next token (round-robin) for the send
                    send_token_row = await _pick_token_round_robin(db)
                    if send_token_row is None:
                        logger.warning('realtime_listener: no active tokens to send with')
                        break

                    send_proxy: str | None = None
                    if send_token_row.proxy_host and send_token_row.proxy_port:
                        send_proxy = token_manager.build_proxy_url(
                            host=send_token_row.proxy_host,
                            port=send_token_row.proxy_port,
                            username=send_token_row.proxy_username or '',
                            password=send_token_row.proxy_password or '',
                        )

                    event = RealtimeTransferEvent(
                        source_channel_id=mapping.source_channel_id,
                        target_channel_id=mapping.target_channel_id,
                        source_message_id=message['id'],
                        source_author=author_name,
                        content=content,
                        token_id=send_token_row.id,
                        token_label=send_token_row.label,
                        status='pending',
                    )
                    db.add(event)
                    db.flush()

                    try:
                        result = await discord_client.send_message(
                            channel_id=mapping.target_channel_id,
                            content=content,
                            token=send_token_row.token_value,
                            proxy_url=send_proxy,
                        )
                        if result.get('status') == 'sent':
                            event.status = 'sent'
                            _stats['transferred'] = _stats.get('transferred', 0) + 1
                            _stats['last_transfer'] = datetime.now(timezone.utc).isoformat()
                            log_event(
                                'realtime_transfer_sent',
                                {
                                    'source_channel': mapping.source_channel_id,
                                    'target_channel': mapping.target_channel_id,
                                    'token_label': send_token_row.label,
                                    'message_id': message['id'],
                                },
                            )
                        else:
                            event.status = 'failed'
                            event.error = f"{result.get('status')}: {result.get('detail', '')}"
                            _stats['failed'] = _stats.get('failed', 0) + 1
                            logger.warning(
                                'realtime_listener: send failed ch=%s token=%s: %s',
                                mapping.target_channel_id,
                                send_token_row.label,
                                event.error,
                            )
                    except Exception as exc:
                        event.status = 'failed'
                        event.error = str(exc)
                        _stats['failed'] = _stats.get('failed', 0) + 1
                        logger.error('realtime_listener: exception sending: %s', exc, exc_info=True)

                    db.commit()
                    # Brief delay between individual message sends to respect rate limits
                    await asyncio.sleep(0.6 + random.uniform(0.0, 0.4))

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error('realtime_listener: loop error: %s', exc, exc_info=True)
        finally:
            db.close()

        await asyncio.sleep(_interval_ms / 1000.0)

    logger.info('realtime_listener: loop stopped')
