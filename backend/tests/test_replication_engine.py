from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.research import AccountToken, AppSetting, ChannelMapping, MessagePattern, ReplicationQueueItem
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


def _seed_mapping_only(db):
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


def _seed_bot_runtype(db, bot_token: str = 'my-bot-token'):
    db.add(AppSetting(key='runtype', value='BOTT'))
    db.add(AppSetting(key='discord_bot_token', value=bot_token))
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


def test_run_session_usert_mode_uses_user_token(monkeypatch):
    """USERT mode must pick a user AccountToken and record it in the payload."""
    db = _make_db()
    _seed_token_and_mapping(db)
    engine = ConversationReplicationEngine()

    monkeypatch.setattr(
        engine,
        '_fetch_source_channel_messages',
        lambda *args, **kwargs: [
            {'content': 'hello', 'source_author_hash': 'h1', 'source_message_id': '1', 'source_created_at': None},
        ],
    )

    session, generated = engine.run_session(
        db=db,
        source_guild_id='src-guild',
        target_guild_id='tgt-guild',
        turn_count=1,
        context_tag_trigger='@',
    )

    assert session.status == 'completed'
    queued = db.query(ReplicationQueueItem).all()
    assert len(queued) == 1
    # User-token mode: delivery_mode=token, no webhook_identity
    assert queued[0].payload['delivery_mode'] == 'token'
    assert queued[0].payload['webhook_identity'] == {}
    # Sender label comes from the AccountToken seeded above
    assert queued[0].payload['responder_account_label'] == 'token-1'
    assert queued[0].payload['responder_account_id'] is not None


def test_run_session_bott_mode_without_user_tokens(monkeypatch):
    """BOTT mode must complete even when no AccountToken (user token) rows exist."""
    db = _make_db()
    _seed_mapping_only(db)
    _seed_bot_runtype(db)
    engine = ConversationReplicationEngine()

    monkeypatch.setattr(
        engine,
        '_fetch_source_channel_messages',
        lambda *args, **kwargs: [
            {'content': 'bot sourced msg', 'source_author_hash': 'h1', 'source_message_id': '10', 'source_created_at': None},
            {'content': 'bot sourced msg 2', 'source_author_hash': 'h2', 'source_message_id': '11', 'source_created_at': None},
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
    assert len(generated) == 2
    queued = db.query(ReplicationQueueItem).order_by(ReplicationQueueItem.id.asc()).all()
    assert len(queued) == 2
    # BOTT mode should use webhook delivery with DFA Mirror identity
    assert queued[0].payload['delivery_mode'] == 'webhook'
    assert queued[0].payload['webhook_identity']['username'] == 'DFA Mirror'
    # No user token means account fields are None
    assert queued[0].payload['responder_account_id'] is None


def test_run_session_bott_mode_with_user_tokens(monkeypatch):
    """BOTT mode picks user token label for the webhook identity when tokens exist."""
    db = _make_db()
    _seed_token_and_mapping(db)
    _seed_bot_runtype(db)
    engine = ConversationReplicationEngine()

    monkeypatch.setattr(
        engine,
        '_fetch_source_channel_messages',
        lambda *args, **kwargs: [
            {'content': 'msg', 'source_author_hash': 'h1', 'source_message_id': '99', 'source_created_at': None},
        ],
    )

    session, generated = engine.run_session(
        db=db,
        source_guild_id='src-guild',
        target_guild_id='tgt-guild',
        turn_count=1,
        context_tag_trigger='@',
    )

    assert session.status == 'completed'
    queued = db.query(ReplicationQueueItem).all()
    assert queued[0].payload['delivery_mode'] == 'webhook'
    # When a user token exists, its label is used as the webhook username
    assert queued[0].payload['webhook_identity']['username'] == 'token-1'


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


def test_normalize_bot_auth_no_prefix():
    engine = ConversationReplicationEngine()
    assert engine._normalize_bot_auth('mytoken123') == 'Bot mytoken123'


def test_normalize_bot_auth_already_prefixed():
    engine = ConversationReplicationEngine()
    assert engine._normalize_bot_auth('Bot mytoken123') == 'Bot mytoken123'


def test_normalize_bot_auth_case_insensitive_prefix():
    engine = ConversationReplicationEngine()
    result = engine._normalize_bot_auth('BOT mytoken123')
    # Should not double-prefix regardless of case
    assert not result.lower().startswith('bot bot ')
    assert 'mytoken123' in result
