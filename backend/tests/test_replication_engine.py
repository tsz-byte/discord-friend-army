from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.research import AccountToken, ChannelMapping, MessagePattern, ReplicationQueueItem
from app.services.replication_engine import ConversationReplicationEngine


def _make_db():
    engine = create_engine('sqlite:///:memory:', future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def _seed_token_and_mapping(db):
    db.add(
        AccountToken(
            label='token-1',
            token_value='token-value',
            token_hash='hash-token-1',
            is_active=True,
            health_status='healthy',
            rotation_priority=1,
            usage_count=0,
        )
    )
    db.add(
        ChannelMapping(
            source_guild_id='src-guild',
            source_channel_id='src-channel',
            target_guild_id='tgt-guild',
            target_channel_id='tgt-channel',
            enabled=True,
            filters={},
            settings={},
        )
    )
    db.commit()


def test_run_session_uses_source_channel_history(monkeypatch):
    db = _make_db()
    _seed_token_and_mapping(db)
    engine = ConversationReplicationEngine()

    monkeypatch.setattr(
        engine,
        '_fetch_source_channel_messages',
        lambda *args, **kwargs: [
            {
                'content': 'real message one',
                'source_author_hash': 'author-hash-1',
                'source_message_id': '111',
                'source_created_at': '2026-04-22T00:00:00Z',
            },
            {
                'content': 'real message two',
                'source_author_hash': 'author-hash-2',
                'source_message_id': '222',
                'source_created_at': '2026-04-22T00:00:05Z',
            },
        ],
    )

    session, generated = engine.run_session(
        db=db,
        source_guild_id='src-guild',
        target_guild_id='tgt-guild',
        turn_count=2,
        context_tag_trigger='@',
    )

    assert session.status == 'completed'
    assert session.mode == 'replication'
    assert [item['content'] for item in generated] == ['real message one', 'real message two']

    queued = db.query(ReplicationQueueItem).order_by(ReplicationQueueItem.id.asc()).all()
    assert len(queued) == 2
    assert queued[0].payload['source_message_id'] == '111'
    assert queued[0].payload['source_created_at'] == '2026-04-22T00:00:00Z'
    assert queued[0].payload['source_content'] == 'real message one'
    assert 'placeholder' not in queued[0].payload['source_content'].lower()


def test_run_session_falls_back_to_pattern_samples_without_placeholder(monkeypatch):
    db = _make_db()
    _seed_token_and_mapping(db)
    db.add(
        MessagePattern(
            source_guild_id='src-guild',
            author_hash='pattern-author',
            style_vector={},
            sample_messages=['pattern text one'],
            active_hours=[],
            mention_likelihood=0,
        )
    )
    db.commit()

    engine = ConversationReplicationEngine()
    monkeypatch.setattr(engine, '_fetch_source_channel_messages', lambda *args, **kwargs: [])

    session, generated = engine.run_session(
        db=db,
        source_guild_id='src-guild',
        target_guild_id='tgt-guild',
        turn_count=1,
        context_tag_trigger='@',
    )

    assert session.status == 'completed'
    assert generated[0]['content'] == 'pattern text one'
    assert 'placeholder' not in generated[0]['content'].lower()


def test_run_session_fails_when_no_source_or_pattern_messages(monkeypatch):
    db = _make_db()
    _seed_token_and_mapping(db)
    engine = ConversationReplicationEngine()

    monkeypatch.setattr(engine, '_fetch_source_channel_messages', lambda *args, **kwargs: [])

    session, generated = engine.run_session(
        db=db,
        source_guild_id='src-guild',
        target_guild_id='tgt-guild',
        turn_count=1,
        context_tag_trigger='@',
    )

    assert session.status == 'failed'
    assert generated == []
    assert session.session_metrics['reason'] == 'missing_source_messages'
