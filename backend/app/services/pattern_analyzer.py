from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy.orm import Session

from app.models.research import MessagePattern, MessageResearchEvent


class MessagePatternAnalyzer:
    def capture_patterns(self, db: Session, source_guild_id: str, min_messages_per_user: int, max_patterns: int) -> list[MessagePattern]:
        rows = (
            db.query(MessageResearchEvent)
            .filter(MessageResearchEvent.guild_id == source_guild_id)
            .order_by(MessageResearchEvent.created_at.desc())
            .all()
        )

        grouped: dict[str, list[MessageResearchEvent]] = defaultdict(list)
        for row in rows:
            grouped[row.author_hash].append(row)

        created: list[MessagePattern] = []
        for author_hash, messages in grouped.items():
            if len(messages) < min_messages_per_user:
                continue

            samples = [m.content_excerpt for m in messages[:5] if m.content_excerpt]
            sentiment_mix = Counter(m.sentiment for m in messages)
            active_hours = sorted({m.created_at.hour for m in messages})
            mention_edges = sum(len(m.interaction_edges) for m in messages)
            mention_likelihood = round((mention_edges / max(len(messages), 1)) * 100)

            style_vector = {
                'message_count': len(messages),
                'avg_excerpt_length': round(sum(len(s) for s in samples) / max(len(samples), 1), 2),
                'sentiment_mix': dict(sentiment_mix),
            }

            pattern = db.query(MessagePattern).filter(
                MessagePattern.source_guild_id == source_guild_id,
                MessagePattern.author_hash == author_hash,
            ).first()
            if pattern is None:
                pattern = MessagePattern(
                    source_guild_id=source_guild_id,
                    author_hash=author_hash,
                    style_vector=style_vector,
                    sample_messages=samples,
                    active_hours=active_hours,
                    mention_likelihood=mention_likelihood,
                )
                db.add(pattern)
            else:
                pattern.style_vector = style_vector
                pattern.sample_messages = samples
                pattern.active_hours = active_hours
                pattern.mention_likelihood = mention_likelihood
            created.append(pattern)

            if len(created) >= max_patterns:
                break

        db.commit()
        for item in created:
            db.refresh(item)
        return created
