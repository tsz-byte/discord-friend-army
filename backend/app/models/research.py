from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String, Text, func

from app.db.session import Base


class GuildOptIn(Base):
    __tablename__ = 'guild_opt_in'

    id = Column(Integer, primary_key=True, index=True)
    guild_id = Column(String(32), unique=True, nullable=False, index=True)
    guild_name = Column(String(255), nullable=False)
    opted_in = Column(Boolean, default=True, nullable=False)
    methodology_version = Column(String(32), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class UserPrivacyPreference(Base):
    __tablename__ = 'user_privacy_preference'

    id = Column(Integer, primary_key=True, index=True)
    guild_id = Column(String(32), nullable=False, index=True)
    user_hash = Column(String(128), nullable=False, index=True)
    include_in_research = Column(Boolean, default=True, nullable=False)
    retention_days = Column(Integer, default=90)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MessageResearchEvent(Base):
    __tablename__ = 'message_research_event'

    id = Column(Integer, primary_key=True, index=True)
    guild_id = Column(String(32), nullable=False, index=True)
    channel_id = Column(String(32), nullable=False, index=True)
    author_hash = Column(String(128), nullable=False, index=True)
    sentiment = Column(String(16), nullable=False)
    topics = Column(JSON, nullable=False, default=list)
    interaction_edges = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    event_metadata = Column('metadata', JSON, nullable=False, default=dict)
    content_excerpt = Column(Text, nullable=True)


class AccountToken(Base):
    __tablename__ = 'account_token'

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String(128), nullable=False)
    token_value = Column(Text, nullable=False)
    source_identity = Column(String(255), nullable=True)
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    proxy_host = Column(String(255), nullable=True)
    proxy_port = Column(Integer, nullable=True)
    proxy_username = Column(String(255), nullable=True)
    proxy_password = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    health_status = Column(String(32), default='unknown', nullable=False)
    health_checked_at = Column(DateTime(timezone=True), nullable=True)
    rotation_priority = Column(Integer, default=100, nullable=False)
    usage_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ServerConnection(Base):
    __tablename__ = 'server_connection'

    id = Column(Integer, primary_key=True, index=True)
    guild_id = Column(String(32), nullable=False, index=True)
    guild_name = Column(String(255), nullable=False)
    role = Column(String(16), nullable=False, index=True)  # source or target
    invite_link = Column(String(512), nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)
    joined_status = Column(String(32), default='pending', nullable=False)
    research_scope = Column(String(255), nullable=False, default='educational_replication')
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MessagePattern(Base):
    __tablename__ = 'message_pattern'

    id = Column(Integer, primary_key=True, index=True)
    source_guild_id = Column(String(32), nullable=False, index=True)
    author_hash = Column(String(128), nullable=False, index=True)
    style_vector = Column(JSON, nullable=False, default=dict)
    sample_messages = Column(JSON, nullable=False, default=list)
    active_hours = Column(JSON, nullable=False, default=list)
    mention_likelihood = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ReplicationSession(Base):
    __tablename__ = 'replication_session'

    id = Column(Integer, primary_key=True, index=True)
    source_guild_id = Column(String(32), nullable=False, index=True)
    target_guild_id = Column(String(32), nullable=False, index=True)
    mode = Column(String(32), nullable=False, default='educational_controlled')
    status = Column(String(32), nullable=False, default='idle')
    account_plan = Column(JSON, nullable=False, default=list)
    session_metrics = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ChannelMapping(Base):
    __tablename__ = 'channel_mapping'

    id = Column(Integer, primary_key=True, index=True)
    source_guild_id = Column(String(32), nullable=False, index=True)
    source_channel_id = Column(String(32), nullable=False, index=True)
    target_guild_id = Column(String(32), nullable=False, index=True)
    target_channel_id = Column(String(32), nullable=False, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    filters = Column(JSON, nullable=False, default=dict)
    settings = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ReplicationQueueItem(Base):
    __tablename__ = 'replication_queue_item'

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False, index=True)
    source_guild_id = Column(String(32), nullable=False, index=True)
    source_channel_id = Column(String(32), nullable=False, index=True)
    target_guild_id = Column(String(32), nullable=False, index=True)
    target_channel_id = Column(String(32), nullable=False, index=True)
    payload = Column(JSON, nullable=False, default=dict)
    status = Column(String(32), nullable=False, default='queued')
    attempts = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
    queued_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    processed_at = Column(DateTime(timezone=True), nullable=True, index=True)


class CoordinationEvent(Base):
    __tablename__ = 'coordination_event'

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False, index=True)
    trigger_account_label = Column(String(128), nullable=False)
    responder_account_label = Column(String(128), nullable=False)
    reason = Column(String(255), nullable=False, default='mention_trigger')
    event_metadata = Column('metadata', JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ConversationMirrorEvent(Base):
    __tablename__ = 'conversation_mirror_event'

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, nullable=False, index=True)
    source_channel_id = Column(String(32), nullable=False, index=True)
    target_channel_id = Column(String(32), nullable=False, index=True)
    source_content = Column(Text, nullable=False)
    replicated_content = Column(Text, nullable=False)
    source_author_hash = Column(String(128), nullable=False, index=True)
    responder_account_label = Column(String(128), nullable=False)
    response_time_ms = Column(Integer, nullable=False, default=0)
    replicated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class ProxyEntry(Base):
    __tablename__ = 'proxy_entry'

    id = Column(Integer, primary_key=True, index=True)
    host = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False)
    username = Column(String(255), nullable=False)
    password = Column(String(255), nullable=False)
    is_healthy = Column(Boolean, default=True, nullable=False)
    health_checked_at = Column(DateTime(timezone=True), nullable=True)
    success_count = Column(Integer, default=0, nullable=False)
    failure_count = Column(Integer, default=0, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RealtimeTransferEvent(Base):
    __tablename__ = 'realtime_transfer_event'

    id = Column(Integer, primary_key=True, index=True)
    source_channel_id = Column(String(32), nullable=False, index=True)
    target_channel_id = Column(String(32), nullable=False, index=True)
    source_message_id = Column(String(32), nullable=False, index=True)
    source_author = Column(String(255), nullable=True)
    content = Column(Text, nullable=False)
    token_id = Column(Integer, nullable=True, index=True)
    token_label = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default='pending')
    error = Column(Text, nullable=True)
    transferred_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class ServerJoinHistory(Base):
    __tablename__ = 'server_join_history'

    id = Column(Integer, primary_key=True, index=True)
    token_id = Column(Integer, nullable=True, index=True)
    guild_id = Column(String(32), nullable=True, index=True)
    invite_code = Column(String(128), nullable=False, index=True)
    status = Column(String(32), nullable=False, default='pending')
    error = Column(Text, nullable=True)
    joined_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class CaptchaChallenge(Base):
    __tablename__ = 'captcha_challenge'

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String(128), nullable=True, index=True)
    anysolver_session_id = Column(String(128), nullable=True, index=True)
    token_id = Column(Integer, nullable=True, index=True)
    guild_id = Column(String(32), nullable=True, index=True)
    challenge_type = Column(String(64), nullable=False, default='hcaptcha')
    sitekey = Column(String(255), nullable=True)
    rqdata = Column(Text, nullable=True)
    solver_status = Column(String(32), nullable=False, default='processing', index=True)
    solved_token = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    cost_usd = Column(String(32), nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class ClanTagHistory(Base):
    __tablename__ = 'clan_tag_history'

    id = Column(Integer, primary_key=True, index=True)
    token_id = Column(Integer, nullable=False, index=True)
    previous_tag = Column(String(100), nullable=True)
    new_tag = Column(String(100), nullable=True)
    status = Column(String(32), nullable=False, default='pending')
    error = Column(Text, nullable=True)
    changed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class NicknameHistory(Base):
    __tablename__ = 'nickname_history'

    id = Column(Integer, primary_key=True, index=True)
    token_id = Column(Integer, nullable=False, index=True)
    guild_id = Column(String(32), nullable=False, index=True)
    nickname = Column(String(32), nullable=True)
    status = Column(String(32), nullable=False, default='pending')
    error = Column(Text, nullable=True)
    changed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class MimicProfile(Base):
    __tablename__ = 'mimic_profile'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(32), nullable=False, index=True)
    guild_id = Column(String(32), nullable=False, index=True)
    analysis_depth = Column(Integer, nullable=False, default=100)
    avg_message_length = Column(Integer, nullable=False, default=0)
    common_words = Column(JSON, nullable=False, default=list)
    active_hours = Column(JSON, nullable=False, default=list)
    emoji_usage = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class ConversationTransferHistory(Base):
    __tablename__ = 'conversation_transfer_history'

    id = Column(Integer, primary_key=True, index=True)
    source_guild_id = Column(String(32), nullable=False, index=True)
    source_channel_id = Column(String(32), nullable=False, index=True)
    target_guild_id = Column(String(32), nullable=False, index=True)
    target_channel_id = Column(String(32), nullable=False, index=True)
    transfer_mode = Column(String(32), nullable=False, default='exact')
    status = Column(String(32), nullable=False, default='pending')
    messages_sent = Column(Integer, nullable=False, default=0)
    errors = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class AppSetting(Base):
    """Runtime-editable key/value settings stored in the database.

    These override environment variables without requiring a restart.
    """

    __tablename__ = 'app_setting'

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(128), nullable=False, unique=True, index=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
