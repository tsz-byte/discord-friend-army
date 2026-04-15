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
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
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
