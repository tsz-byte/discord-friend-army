"""Background auto-replication loop.

This module manages a persistent asyncio task that:
  1. Re-queues eligible failed items (up to 3 attempts).
  2. Fetches current target-guild members so replies can tag real users.
  3. Runs a replication session for every enabled channel mapping.
  4. Dispatches all ``queued`` items to Discord (real HTTP sends).
  5. Sleeps for ``interval_seconds`` before the next cycle.

The loop is started during FastAPI startup and can be toggled at runtime
via the ``/api/v1/replication/auto-loop/toggle`` endpoint.

Architecture note: replication sessions created by ``run_session`` leave all
messages in ``status='queued'``.  The actual Discord HTTP sends are deferred to
``_dispatch_queued_items`` which runs in this async background loop (or on
``/replication/queue/retry-failed``).  This separation keeps the synchronous
session-creation path free of async I/O and lets the rate-limited dispatch run
at its own pace.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.services.activity_logger import log_event

logger = logging.getLogger('discord_research.auto_replication')

# How long to wait after FastAPI startup before running the first cycle.
# This gives the DB migrations, seeding, and any other startup hooks time to
# finish before we start making Discord API calls.
_SERVER_INIT_DELAY_SECONDS = 12

# ---------------------------------------------------------------------------
# Module-level state (single-process only; fine for this use-case)
# ---------------------------------------------------------------------------
_auto_loop_enabled: bool = False
_loop_task: asyncio.Task | None = None
_loop_interval_seconds: int = 180  # default: 3 minutes


def _read_setting_value(db: Session, key: str) -> str | None:
    try:
        from app.models.research import AppSetting
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row and row.value is not None:
            return row.value
    except Exception:
        pass
    return None


def _read_runtype(db: Session) -> str:
    value = (_read_setting_value(db, 'runtype') or '').strip().upper()
    if value in {'USERT', 'BOTT'}:
        return value
    from app.core.config import get_settings

    return get_settings().runtype


def _read_bot_token(db: Session) -> str:
    value = (_read_setting_value(db, 'discord_bot_token') or '').strip()
    if value:
        return value
    from app.core.config import get_settings

    return (get_settings().discord_bot_token or '').strip()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _dispatch_queued_items(db: Session) -> int:
    """Send all ``queued`` items to Discord.  Returns the number dispatched."""
    from app.models.research import ReplicationQueueItem
    from app.services.discord_client import DiscordClient
    from app.services.token_manager import TokenManagerService

    discord_client = DiscordClient()
    token_manager = TokenManagerService()

    items = (
        db.query(ReplicationQueueItem)
        .filter(ReplicationQueueItem.status == 'queued', ReplicationQueueItem.attempts < 3)
        .order_by(ReplicationQueueItem.id.asc())
        .limit(50)
        .all()
    )

    dispatched = 0
    runtype = _read_runtype(db)
    bot_token = _read_bot_token(db)
    for item in items:
        token = None
        if runtype == 'USERT':
            token = token_manager.pick_for_rotation(db)
            if token is None:
                item.status = 'failed'
                item.error = 'No active tokens available for dispatch'
                item.attempts += 1
                item.processed_at = datetime.now(timezone.utc)
                db.commit()
                logger.error('dispatch_queued_items: no active tokens, item %d failed', item.id)
                break

        proxy_url: str | None = None
        if token and token.proxy_host and token.proxy_port:
            proxy_url = token_manager.build_proxy_url(
                host=token.proxy_host,
                port=token.proxy_port,
                username=token.proxy_username or '',
                password=token.proxy_password or '',
            )

        content: str = item.payload.get('replicated_content', 'Hello!')
        target_channel_id: str = item.target_channel_id

        try:
            if runtype == 'BOTT':
                identity = item.payload.get('webhook_identity', {}) if isinstance(item.payload, dict) else {}
                result = await discord_client.send_webhook_message(
                    channel_id=target_channel_id,
                    content=content,
                    username=identity.get('username', 'DFA Mirror'),
                    avatar_url=identity.get('avatar_url'),
                    bot_token=bot_token,
                )
            else:
                result = await discord_client.send_message(
                    channel_id=target_channel_id,
                    content=content,
                    token=token.token_value,
                    proxy_url=proxy_url,
                )
        except Exception as exc:
            result = {'status': 'error', 'detail': str(exc)}
            logger.error('dispatch_queued_items: unexpected error sending item %d: %s', item.id, exc)

        item.attempts += 1
        item.processed_at = datetime.now(timezone.utc)

        if result['status'] in ('sent',):
            item.status = 'dispatched'
            item.error = None
            dispatched += 1
        elif (
            runtype == 'USERT'
            and token is not None
            and result.get('status') == 'failed'
            and result.get('code') == 403
            and '50001' in str(result.get('detail', ''))
        ):
            onboarding_ok = await discord_client.complete_onboarding(
                guild_id=item.target_guild_id,
                token=token.token_value,
                proxy_url=proxy_url,
            )
            if onboarding_ok:
                item.status = 'queued'
                item.error = 'missing_access: onboarding completed, queued for retry'
            else:
                item.status = 'failed'
                item.error = 'missing_access: onboarding retry failed'
            logger.warning('dispatch_queued_items: item %d missing access (onboarding_ok=%s)', item.id, onboarding_ok)
            db.commit()
            continue
        elif result.get('code') == 429:
            # Rate-limited: leave queued so it is retried next cycle.
            item.status = 'queued'
            logger.warning('dispatch_queued_items: rate-limited on item %d, will retry', item.id)
            db.commit()
            break  # Stop dispatching this cycle to respect rate limits
        else:
            item.status = 'failed'
            item.error = f"{result.get('status')}: {result.get('detail', '')}"
            if token is not None and result.get('code') == 401:
                token.health_status = 'invalid'
            logger.error(
                'dispatch_queued_items: item %d failed — %s',
                item.id,
                item.error,
            )

        db.commit()

        # Small courtesy delay between messages to avoid rate-limit bursts.
        await asyncio.sleep(1.5)

    return dispatched


async def _run_loop_cycle() -> None:
    """Execute one full auto-replication cycle."""
    from app.db.session import SessionLocal
    from app.models.research import AccountToken, ChannelMapping, ReplicationQueueItem
    from app.services.discord_client import DiscordClient
    from app.services.replication_engine import ConversationReplicationEngine
    from app.services.token_manager import TokenManagerService

    db = SessionLocal()
    try:
        # ---- 1. Re-queue eligible failed items ----
        failed_items = (
            db.query(ReplicationQueueItem)
            .filter(ReplicationQueueItem.status == 'failed', ReplicationQueueItem.attempts < 3)
            .limit(50)
            .all()
        )
        if failed_items:
            for item in failed_items:
                item.status = 'queued'
            db.commit()
            logger.info('auto_loop: re-queued %d failed items', len(failed_items))

        # ---- 2. Check readiness: need tokens + channel mappings ----
        runtype = _read_runtype(db)
        active_tokens = db.query(AccountToken).filter(AccountToken.is_active.is_(True)).count()
        mappings = (
            db.query(ChannelMapping)
            .filter(ChannelMapping.enabled.is_(True))
            .all()
        )

        if runtype == 'USERT' and active_tokens == 0:
            logger.warning('auto_loop: skipping cycle — no active tokens')
            return

        if not mappings:
            logger.warning('auto_loop: skipping cycle — no enabled channel mappings')
            return

        # ---- 3. Fetch target-guild members for tagging (best-effort) ----
        token_manager = TokenManagerService()
        discord_client = DiscordClient()
        first_token = token_manager.pick_for_rotation(db) if runtype == 'USERT' else None
        target_members: list[str] = []
        if runtype == 'USERT' and first_token:
            target_guild_ids = list({m.target_guild_id for m in mappings})
            proxy_url: str | None = None
            if first_token.proxy_host and first_token.proxy_port:
                proxy_url = token_manager.build_proxy_url(
                    host=first_token.proxy_host,
                    port=first_token.proxy_port,
                    username=first_token.proxy_username or '',
                    password=first_token.proxy_password or '',
                )
            for gid in target_guild_ids[:2]:
                members = await discord_client.get_guild_members(
                    guild_id=gid,
                    token=first_token.token_value,
                    proxy_url=proxy_url,
                )
                target_members.extend(members)

        # ---- 4. Run replication sessions ----
        engine = ConversationReplicationEngine()
        for mapping in mappings[:5]:  # cap at 5 mappings per cycle
            try:
                tag_prob = _read_setting_float(db, 'tag_probability', 0.20)
                session, generated = engine.run_session(
                    db=db,
                    source_guild_id=mapping.source_guild_id,
                    target_guild_id=mapping.target_guild_id,
                    turn_count=5,
                    context_tag_trigger='@',
                    target_members=target_members or None,
                    tag_probability=tag_prob,
                )
                log_event('auto_loop_cycle', {
                    'session_id': session.id,
                    'source': mapping.source_guild_id,
                    'target': mapping.target_guild_id,
                    'generated': len(generated),
                })
            except Exception as exc:
                logger.error('auto_loop: session failed for mapping %d: %s', mapping.id, exc)

        # ---- 5. Dispatch queued items to Discord ----
        dispatched = await _dispatch_queued_items(db)
        if dispatched > 0:
            logger.info('auto_loop: dispatched %d messages to Discord', dispatched)

    except Exception as exc:
        logger.error('auto_loop: unhandled cycle error: %s', exc, exc_info=True)
    finally:
        db.close()


def _read_setting_float(db: Session, key: str, default: float) -> float:
    """Read a float setting from the AppSetting table with a fallback default."""
    try:
        from app.models.research import AppSetting
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row and row.value is not None:
            return float(row.value)
    except Exception:
        pass
    return default


async def _auto_loop() -> None:
    """Main background coroutine — runs indefinitely until cancelled."""
    global _auto_loop_enabled

    await asyncio.sleep(_SERVER_INIT_DELAY_SECONDS)
    logger.info('auto_loop: started (interval=%ds)', _loop_interval_seconds)

    while _auto_loop_enabled:
        try:
            await _run_loop_cycle()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error('auto_loop: top-level error: %s', exc, exc_info=True)

        # Wait for next cycle, but check the enabled flag every second so we
        # can stop promptly when requested.
        for _ in range(_loop_interval_seconds):
            if not _auto_loop_enabled:
                break
            await asyncio.sleep(1)

    logger.info('auto_loop: stopped')


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_status() -> dict:
    """Return the current auto-loop state as a dict."""
    return {
        'enabled': _auto_loop_enabled,
        'interval_seconds': _loop_interval_seconds,
        'task_alive': _loop_task is not None and not _loop_task.done(),
    }


def start_loop(interval_seconds: int = 180) -> dict:
    """Enable the auto-loop, (re-)creating the background task if needed."""
    global _auto_loop_enabled, _loop_task, _loop_interval_seconds
    _loop_interval_seconds = max(30, interval_seconds)
    _auto_loop_enabled = True
    if _loop_task is None or _loop_task.done():
        _loop_task = asyncio.create_task(_auto_loop())
        logger.info('auto_loop: task created (interval=%ds)', _loop_interval_seconds)
    return get_status()


def stop_loop() -> dict:
    """Disable the auto-loop and cancel the background task."""
    global _auto_loop_enabled, _loop_task
    _auto_loop_enabled = False
    if _loop_task and not _loop_task.done():
        _loop_task.cancel()
        _loop_task = None
        logger.info('auto_loop: task cancelled')
    return get_status()
