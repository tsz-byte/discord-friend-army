from datetime import datetime

from pydantic import BaseModel, Field


class GuildOptInRequest(BaseModel):
    guild_id: str
    guild_name: str
    methodology_version: str = '2026.04'


class GuildOptInResponse(BaseModel):
    guild_id: str
    opted_in: bool
    updated_at: datetime


class MessageIngestRequest(BaseModel):
    guild_id: str
    channel_id: str
    author_id: str
    message_id: str
    content: str = Field(min_length=1, max_length=4000)
    mentions: list[str] = Field(default_factory=list)
    created_at: datetime


class AnalyticsOverview(BaseModel):
    guild_id: str
    total_messages: int
    active_users: int
    avg_sentiment_score: float
    top_topics: list[dict]


class UserPrivacyRequest(BaseModel):
    guild_id: str
    user_id: str
    include_in_research: bool = False
    retention_days: int = 30


class ComplianceMethodology(BaseModel):
    methodology_version: str
    consent_model: str
    anonymization: str
    retention_policy: str
    publication_support: list[str]
