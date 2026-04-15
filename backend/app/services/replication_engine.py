from __future__ import annotations

import random

from sqlalchemy.orm import Session

from app.models.research import AccountToken, MessagePattern, ReplicationSession
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

        session = ReplicationSession(
            source_guild_id=source_guild_id,
            target_guild_id=target_guild_id,
            mode='educational_controlled',
            status='running',
            account_plan=[{'id': t.id, 'label': t.label} for t in account_pool],
            session_metrics={'turn_count': turn_count, 'generated_count': 0},
        )
        db.add(session)
        db.commit()
        db.refresh(session)

        generated: list[dict] = []
        for i in range(turn_count):
            token = self.token_manager.pick_for_rotation(db)
            if token is None:
                break

            pattern = random.choice(patterns) if patterns else None
            sample = random.choice(pattern.sample_messages) if pattern and pattern.sample_messages else 'Educational replication placeholder.'
            if i > 0 and i % 3 == 0:
                prev = generated[-1]
                sample = f"{context_tag_trigger}{prev['account_label']} {sample}"

            generated.append(
                {
                    'turn': i + 1,
                    'account_id': token.id,
                    'account_label': token.label,
                    'content': sample,
                    'context_aware': context_tag_trigger in sample,
                }
            )

        session.status = 'completed'
        session.session_metrics = {
            'turn_count': turn_count,
            'generated_count': len(generated),
            'context_aware_count': sum(1 for item in generated if item['context_aware']),
        }
        db.commit()
        db.refresh(session)
        return session, generated
