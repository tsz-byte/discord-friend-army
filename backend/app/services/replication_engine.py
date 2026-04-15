from __future__ import annotations

import random
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.research import (
    AccountToken,
    ChannelMapping,
    ConversationMirrorEvent,
    CoordinationEvent,
    MessagePattern,
    ReplicationQueueItem,
    ReplicationSession,
)
from app.services.token_manager import TokenManagerService


class ConversationReplicationEngine:
    def __init__(self) -> None:
        self.token_manager = TokenManagerService()

    def run_session(
        self,
        db: Session,
        source_guild_id: str,
        target_guild_id: str,
        turn_count: int,
        context_tag_trigger: str,
    ) -> tuple[ReplicationSession, list[dict]]:
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
            mode='educational_controlled',
            status='running',
            account_plan=[{'id': t.id, 'label': t.label} for t in account_pool],
            session_metrics={'turn_count': turn_count, 'generated_count': 0, 'fidelity_score': 0.0},
        )
        db.add(session)
        db.commit()
        db.refresh(session)

        generated: list[dict] = []
        response_times: list[int] = []
        if mapping is None:
            session.status = 'failed'
            session.session_metrics = {'turn_count': turn_count, 'generated_count': 0, 'reason': 'missing_channel_mapping'}
            db.commit()
            db.refresh(session)
            return session, generated

        for i in range(turn_count):
            token = self.token_manager.pick_for_rotation(db)
            if token is None:
                break

            pattern = random.choice(patterns) if patterns else None
            if pattern is not None and pattern.sample_messages:
                base_sample = random.choice(pattern.sample_messages)
            else:
                base_sample = 'Educational replication placeholder.'
            response_time_ms = self._compute_response_time(pattern)

            sample = base_sample
            context_aware = False
            if i > 0 and i % 3 == 0 and generated:
                prev = generated[-1]
                sample = f"{context_tag_trigger}{prev['account_label']} {base_sample}"
                context_aware = True
                self._record_coordination_event(db, session.id, prev['account_label'], token.label, {'turn': i + 1})

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
                    'source_author_hash': pattern.author_hash if pattern else 'anonymized-source',
                    'responder_account_id': token.id,
                    'responder_account_label': token.label,
                    'context_aware': context_aware,
                    'response_time_ms': response_time_ms,
                },
                status='queued',
            )
            db.add(queue_item)
            db.commit()
            db.refresh(queue_item)

            processed_item = self._process_queue_item(db, queue_item)
            if processed_item.status != 'processed':
                continue

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
                    source_author_hash=pattern.author_hash if pattern else 'anonymized-source',
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
    def _process_queue_item(db: Session, item: ReplicationQueueItem) -> ReplicationQueueItem:
        item.attempts += 1
        try:
            item.status = 'processed'
            item.processed_at = datetime.now(timezone.utc)
            item.error = None
        except Exception as exc:  # pragma: no cover
            item.status = 'failed'
            item.error = str(exc)
        db.commit()
        db.refresh(item)
        return item
