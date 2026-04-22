from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.models.research import (
    AccountToken,
    AppSetting,
    ChannelMapping,
    ConversationMirrorEvent,
    CoordinationEvent,
    MessagePattern,
    ReplicationQueueItem,
    ReplicationSession,
)
from app.services.privacy import PrivacyService
from app.services.token_manager import TokenManagerService

logger = logging.getLogger('discord_research.replication_engine')


class ConversationReplicationEngine:
    def __init__(self) -> None:
        self.token_manager = TokenManagerService()
        self.privacy = PrivacyService()

    def run_session(
        self,
        db: Session,
        source_guild_id: str,
        target_guild_id: str,
        turn_count: int,
        context_tag_trigger: str,
        target_members: list[str] | None = None,
        tag_probability: float = 0.20,
    ) -> tuple[ReplicationSession, list[dict]]:
        """Generate a replication session and queue items.

        Args:
            target_members: Optional list of display names from the target guild.
                When provided, each message has a ``tag_probability`` chance of
                mentioning a randomly chosen member.
            tag_probability: Probability (0–1) that a turn will tag a random
                member from ``target_members``.
        """
        patterns = (
            db.query(MessagePattern)
            .filter(MessagePattern.source_guild_id == source_guild_id)
            .order_by(MessagePattern.updated_at.desc())
            .all()
        )
        tokens = db.query(AccountToken).filter(AccountToken.is_active.is_(True)).all()
        account_pool = [t for t in tokens if t.health_status in {'healthy', 'unknown'}]

        mapping = (
            db.query(ChannelMapping)
            .filter(
                ChannelMapping.source_guild_id == source_guild_id,
                ChannelMapping.target_guild_id == target_guild_id,
                ChannelMapping.enabled.is_(True),
            )
            .order_by(ChannelMapping.id.asc())
            .first()
        )

        session = ReplicationSession(
            source_guild_id=source_guild_id,
            target_guild_id=target_guild_id,
            mode='replication',
            status='running',
            account_plan=[
                {'id': t.id, 'label': t.label, 'proxy_host': t.proxy_host, 'proxy_port': t.proxy_port}
                for t in account_pool
            ],
            session_metrics={'turn_count': turn_count, 'generated_count': 0, 'fidelity_score': 0.0},
        )
        db.add(session)
        db.commit()
        db.refresh(session)

        generated: list[dict] = []
        response_times: list[int] = []
        runtype = self._read_runtype(db)
        if mapping is None:
            logger.warning(
                'run_session: no enabled channel mapping for %s → %s; session marked failed',
                source_guild_id,
                target_guild_id,
            )
            session.status = 'failed'
            session.session_metrics = {'turn_count': turn_count, 'generated_count': 0, 'reason': 'missing_channel_mapping'}
            db.commit()
            db.refresh(session)
            return session, generated

        source_pool = self._fetch_source_channel_messages(
            db=db,
            source_guild_id=source_guild_id,
            source_channel_id=mapping.source_channel_id,
            limit=max(20, min(100, turn_count * 4)),
        )
        if not source_pool:
            source_pool = self._source_pool_from_patterns(patterns)
        if not source_pool:
            logger.warning(
                'run_session: no source message history available for channel %s; session marked failed',
                mapping.source_channel_id,
            )
            session.status = 'failed'
            session.session_metrics = {'turn_count': turn_count, 'generated_count': 0, 'reason': 'missing_source_messages'}
            db.commit()
            db.refresh(session)
            return session, generated

        for i in range(turn_count):
            token = self.token_manager.pick_for_rotation(db)
            if token is None:
                logger.warning('run_session: no active tokens remaining at turn %d', i + 1)
                break

            pattern = random.choice(patterns) if patterns else None
            source_entry = source_pool[i % len(source_pool)]
            base_sample = source_entry['content']
            response_time_ms = self._compute_response_time(pattern)

            sample = base_sample
            context_aware = False

            # Every 3rd turn (after the first): create a context-aware reply that
            # mentions the previous speaker so the conversation feels natural.
            if i > 0 and i % 3 == 0 and generated:
                prev = generated[-1]
                sample = f"{context_tag_trigger}{prev['account_label']} {base_sample}"
                context_aware = True
                self._record_coordination_event(db, session.id, prev['account_label'], token.label, {'turn': i + 1})

            # Occasionally tag a random member of the target server to drive engagement.
            elif target_members and random.random() < tag_probability:
                tagged = random.choice(target_members)
                sample = f'@{tagged} {base_sample}'

            queue_item = ReplicationQueueItem(
                session_id=session.id,
                source_guild_id=source_guild_id,
                source_channel_id=mapping.source_channel_id,
                target_guild_id=target_guild_id,
                target_channel_id=mapping.target_channel_id,
                payload={
                    'turn': i + 1,
                    'source_content': base_sample,
                    'replicated_content': sample,
                    'source_author_hash': source_entry.get('source_author_hash', 'anonymized-source'),
                    'source_message_id': source_entry.get('source_message_id'),
                    'source_created_at': source_entry.get('source_created_at'),
                    'responder_account_id': token.id,
                    'responder_account_label': token.label,
                    'context_aware': context_aware,
                    'response_time_ms': response_time_ms,
                    'delivery_mode': 'webhook' if runtype == 'BOTT' else 'token',
                    'webhook_identity': {
                        'username': token.label,
                    } if runtype == 'BOTT' else {},
                },
                # Items start as 'queued'; actual Discord HTTP sends are handled
                # by the async auto-replication dispatcher (auto_replication.py)
                # or the /replication/queue/retry-failed endpoint.
                status='queued',
            )
            db.add(queue_item)
            db.commit()
            db.refresh(queue_item)

            generated_message = {
                'turn': i + 1,
                'account_id': token.id,
                'account_label': token.label,
                'source_channel_id': mapping.source_channel_id,
                'target_channel_id': mapping.target_channel_id,
                'content': sample,
                'context_aware': context_aware,
                'response_time_ms': response_time_ms,
            }
            generated.append(generated_message)
            response_times.append(response_time_ms)

            db.add(
                ConversationMirrorEvent(
                    session_id=session.id,
                    source_channel_id=mapping.source_channel_id,
                    target_channel_id=mapping.target_channel_id,
                    source_content=base_sample,
                    replicated_content=sample,
                    source_author_hash=source_entry.get('source_author_hash', 'anonymized-source'),
                    responder_account_label=token.label,
                    response_time_ms=response_time_ms,
                )
            )
            db.commit()

        session.status = 'completed'
        session.session_metrics = {
            'turn_count': turn_count,
            'generated_count': len(generated),
            'context_aware_count': sum(1 for item in generated if item['context_aware']),
            'avg_response_time_ms': round(sum(response_times) / max(len(response_times), 1), 2),
            'fidelity_score': round(self._fidelity_score(generated, turn_count), 3),
        }
        db.commit()
        db.refresh(session)
        return session, generated

    @staticmethod
    def _compute_response_time(pattern: MessagePattern | None) -> int:
        if pattern is None:
            return 1200
        sentiment_mix = pattern.style_vector.get('sentiment_mix', {}) if isinstance(pattern.style_vector, dict) else {}
        total = sum(sentiment_mix.values()) if isinstance(sentiment_mix, dict) else 0
        base = 600 + (total * 23)
        jitter = random.randint(120, 1100)
        return max(250, min(9000, base + jitter))

    @staticmethod
    def _fidelity_score(generated: list[dict], turn_count: int) -> float:
        if turn_count <= 0:
            return 0.0
        coverage = len(generated) / turn_count
        context_hits = sum(1 for item in generated if item.get('context_aware')) / max(len(generated), 1)
        return (coverage * 0.8) + (context_hits * 0.2)

    @staticmethod
    def _record_coordination_event(db: Session, session_id: int, trigger_account_label: str, responder_account_label: str, metadata: dict) -> None:
        db.add(
            CoordinationEvent(
                session_id=session_id,
                trigger_account_label=trigger_account_label,
                responder_account_label=responder_account_label,
                reason='mention_trigger',
                event_metadata=metadata,
            )
        )
        db.commit()

    @staticmethod
    def _read_runtype(db: Session) -> str:
        row = db.query(AppSetting).filter(AppSetting.key == 'runtype').first()
        if row and row.value:
            value = row.value.strip().upper()
            if value in {'USERT', 'BOTT'}:
                return value
        from app.core.config import get_settings

        return get_settings().runtype

    @staticmethod
    def _read_bot_token(db: Session) -> str:
        row = db.query(AppSetting).filter(AppSetting.key == 'discord_bot_token').first()
        if row and row.value:
            return row.value.strip()
        from app.core.config import get_settings

        return (get_settings().discord_bot_token or '').strip()

    @staticmethod
    def _source_pool_from_patterns(patterns: list[MessagePattern]) -> list[dict]:
        pool: list[dict] = []
        for pattern in patterns:
            for sample in pattern.sample_messages or []:
                text = sample.strip() if isinstance(sample, str) else ''
                if text:
                    pool.append(
                        {
                            'content': text,
                            'source_author_hash': pattern.author_hash,
                            'source_message_id': None,
                            'source_created_at': None,
                        }
                    )
        return pool

    def _fetch_source_channel_messages(
        self,
        db: Session,
        source_guild_id: str,
        source_channel_id: str,
        limit: int,
    ) -> list[dict]:
        runtype = self._read_runtype(db)
        headers: dict[str, str] = {}
        proxy_url: str | None = None

        if runtype == 'BOTT':
            bot_token = self._read_bot_token(db)
            if not bot_token:
                return []
            headers['Authorization'] = f'Bot {bot_token}'
        else:
            source_token = (
                db.query(AccountToken)
                .filter(AccountToken.is_active.is_(True), AccountToken.health_status.in_(['healthy', 'unknown']))
                .order_by(AccountToken.id.asc())
                .first()
            )
            if source_token is None:
                return []
            headers['Authorization'] = source_token.token_value
            if source_token.proxy_host and source_token.proxy_port:
                proxy_url = self.token_manager.build_proxy_url(
                    host=source_token.proxy_host,
                    port=source_token.proxy_port,
                    username=source_token.proxy_username or '',
                    password=source_token.proxy_password or '',
                )

        from app.core.config import get_settings

        base_url = get_settings().discord_api_base_url.rstrip('/')
        try:
            with httpx.Client(timeout=20, proxy=proxy_url) as client:
                response = client.get(
                    f'{base_url}/channels/{source_channel_id}/messages',
                    headers=headers,
                    params={'limit': max(1, min(limit, 100))},
                )
        except httpx.HTTPError as exc:
            logger.warning('run_session: failed to fetch source messages for channel %s: %s', source_channel_id, exc)
            return []

        if response.status_code != 200:
            logger.warning(
                'run_session: source message fetch returned status=%s for channel %s',
                response.status_code,
                source_channel_id,
            )
            return []
        try:
            payload = response.json()
        except ValueError:
            return []
        if not isinstance(payload, list):
            return []

        entries: list[dict] = []
        for message in sorted(payload, key=lambda item: str(item.get('id', '0'))):
            content = (message.get('content') or '').strip()
            if not content:
                attachments = message.get('attachments') if isinstance(message.get('attachments'), list) else []
                content = ' '.join(str(a.get('url', '')).strip() for a in attachments if isinstance(a, dict)).strip()
            if not content:
                continue
            author = message.get('author') if isinstance(message.get('author'), dict) else {}
            author_id = str(author.get('id') or '').strip()
            author_hash = self.privacy.anonymize_user(source_guild_id, author_id) if author_id else 'anonymized-source'
            entries.append(
                {
                    'content': content,
                    'source_author_hash': author_hash,
                    'source_message_id': str(message.get('id') or ''),
                    'source_created_at': message.get('timestamp'),
                }
            )
        return entries
