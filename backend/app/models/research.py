from datetime import datetime

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String, Text

from app.db.session import Base


class GuildOptIn(Base):
    __tablename__ = 'guild_opt_in'

    id = Column(Integer, primary_key=True, index=True)
    guild_id = Column(String(32), unique=True, nullable=False, index=True)
    guild_name = Column(String(255), nullable=False)
    opted_in = Column(Boolean, default=True, nullable=False)
    methodology_version = Column(String(32), nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserPrivacyPreference(Base):
    __tablename__ = 'user_privacy_preference'

    id = Column(Integer, primary_key=True, index=True)
    guild_id = Column(String(32), nullable=False, index=True)
    user_hash = Column(String(128), nullable=False, index=True)
    include_in_research = Column(Boolean, default=True, nullable=False)
    retention_days = Column(Integer, default=90)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MessageResearchEvent(Base):
    __tablename__ = 'message_research_event'

    id = Column(Integer, primary_key=True, index=True)
    guild_id = Column(String(32), nullable=False, index=True)
    channel_id = Column(String(32), nullable=False, index=True)
    author_hash = Column(String(128), nullable=False, index=True)
    sentiment = Column(String(16), nullable=False)
    topics = Column(JSON, nullable=False, default=list)
    interaction_edges = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    event_metadata = Column('metadata', JSON, nullable=False, default=dict)
    content_excerpt = Column(Text, nullable=True)
