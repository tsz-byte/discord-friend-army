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

from app.core.config import get_settings

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

# Timing constants for send rate-limiting (seconds)
_SEND_DELAY_MIN = 0.6
_SEND_DELAY_JITTER = 0.4
_BREAKER_THRESHOLD = 3
_BREAKER_OPEN_SECONDS = 30


def _record_mapping_failure(mapping_id: int, mapping_failures: dict[int, int], mapping_breaker_until: dict[int, float]) -> None:
    mapping_failures[mapping_id] = mapping_failures.get(mapping_id, 0) + 1
    if mapping_failures[mapping_id] >= _BREAKER_THRESHOLD:
        mapping_breaker_until[mapping_id] = datetime.now(timezone.utc).timestamp() + _BREAKER_OPEN_SECONDS


def _read_runtime_mode(db) -> str:
    from app.models.research import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == 'runtype').first()
    if row and row.value:
        value = row.value.strip().upper()
        if value in {'USERT', 'BOTT'}:
            return value
    return get_settings().runtype


def _read_bot_token(db) -> str:
    from app.models.research import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == 'discord_bot_token').first()
    if row and row.value:
        return row.value.strip()
    return (get_settings().discord_bot_token or '').strip()


def _author_avatar_url(author: dict) -> str | None:
    avatar = author.get('avatar')
    author_id = author.get('id')
    if avatar and author_id:
        return f'https://cdn.discordapp.com/avatars/{author_id}/{avatar}.png?size=128'
    return None


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
    mapping_failures: dict[int, int] = {}
    mapping_breaker_until: dict[int, float] = {}

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
                # Default to real-time enabled so all enabled mappings transfer automatically.
                if not settings.get('realtime_enabled', True):
                    continue
                now_ts = datetime.now(timezone.utc).timestamp()
                if mapping_breaker_until.get(mapping.id, 0) > now_ts:
                    continue

                runtype = _read_runtime_mode(db)
                src_proxy: str | None = None
                source_token_row = None
                source_token_value: str | None = None
                bot_token = ''

                if runtype == 'USERT':
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
                    source_token_value = source_token_row.token_value
                    if source_token_row.proxy_host and source_token_row.proxy_port:
                        src_proxy = token_manager.build_proxy_url(
                            host=source_token_row.proxy_host,
                            port=source_token_row.proxy_port,
                            username=source_token_row.proxy_username or '',
                            password=source_token_row.proxy_password or '',
                        )
                else:
                    bot_token = _read_bot_token(db)
                    if not bot_token:
                        logger.warning('realtime_listener: runtype=BOTT but no bot token configured')
                        continue
                    source_token_value = bot_token

                after_id = last_seen.get(mapping.id)
                messages = await discord_client.get_channel_messages(
                    channel_id=mapping.source_channel_id,
                    token=source_token_value,
                    after=after_id,
                    limit=10,
                    proxy_url=src_proxy,
                )
                if source_token_row is not None and source_token_row.health_status == 'unknown':
                    await token_manager.health_check(db, source_token_row)
                if source_token_row is not None and source_token_row.health_status not in {'healthy', 'unknown'}:
                    continue
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
                    discriminator = author.get('discriminator')
                    if discriminator and discriminator != '0':
                        author_name = f'{author_name}#{discriminator}'

                    event = RealtimeTransferEvent(
                        source_channel_id=mapping.source_channel_id,
                        target_channel_id=mapping.target_channel_id,
                        source_message_id=message['id'],
                        source_author=author_name,
                        content=content,
                        token_id=None,
                        token_label='webhook' if runtype == 'BOTT' else None,
                        status='pending',
                    )
                    db.add(event)
                    db.flush()

                    try:
                        if runtype == 'BOTT':
                            result = await discord_client.send_webhook_message(
                                channel_id=mapping.target_channel_id,
                                content=content,
                                username=author_name,
                                avatar_url=_author_avatar_url(author),
                                timestamp_iso=message.get('timestamp'),
                                bot_token=bot_token,
                            )
                        else:
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
                            event.token_id = send_token_row.id
                            event.token_label = send_token_row.label
                            result = await discord_client.send_message(
                                channel_id=mapping.target_channel_id,
                                content=content,
                                token=send_token_row.token_value,
                                proxy_url=send_proxy,
                            )
                        if result.get('status') == 'sent':
                            event.status = 'sent'
                            mapping_failures[mapping.id] = 0
                            _stats['transferred'] = _stats.get('transferred', 0) + 1
                            _stats['last_transfer'] = datetime.now(timezone.utc).isoformat()
                            log_event(
                                'realtime_transfer_sent',
                                {
                                    'source_channel': mapping.source_channel_id,
                                    'target_channel': mapping.target_channel_id,
                                    'token_label': event.token_label,
                                    'message_id': message['id'],
                                    'runtype': runtype,
                                },
                            )
                        else:
                            event.status = 'failed'
                            event.error = f"{result.get('status')}: {result.get('detail', '')}"
                            _stats['failed'] = _stats.get('failed', 0) + 1
                            if runtype == 'USERT' and result.get('code') in (401, 403) and event.token_id:
                                send_token_row = (
                                    db.query(AccountToken)
                                    .filter(AccountToken.id == event.token_id)
                                    .first()
                                )
                                if send_token_row is None:
                                    pass
                                elif result.get('code') == 401:
                                    send_token_row.is_active = False
                                    send_token_row.health_status = 'invalid'
                                else:
                                    send_token_row.health_status = 'invalid'
                            _record_mapping_failure(mapping.id, mapping_failures, mapping_breaker_until)
                            logger.warning(
                                'realtime_listener: send failed ch=%s token=%s: %s',
                                mapping.target_channel_id,
                                event.token_label,
                                event.error,
                            )
                    except Exception as exc:
                        event.status = 'failed'
                        event.error = str(exc)
                        _stats['failed'] = _stats.get('failed', 0) + 1
                        _record_mapping_failure(mapping.id, mapping_failures, mapping_breaker_until)
                        logger.error('realtime_listener: exception sending: %s', exc, exc_info=True)

                    db.commit()
                    # Brief delay between individual message sends to respect rate limits
                    await asyncio.sleep(_SEND_DELAY_MIN + random.uniform(0.0, _SEND_DELAY_JITTER))

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error('realtime_listener: loop error: %s', exc, exc_info=True)
        finally:
            db.close()

        await asyncio.sleep(_interval_ms / 1000.0)

    logger.info('realtime_listener: loop stopped')
