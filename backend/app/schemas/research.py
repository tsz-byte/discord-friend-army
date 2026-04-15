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


class AccountTokenCreateRequest(BaseModel):
    label: str = Field(min_length=2, max_length=128)
    token_value: str = Field(min_length=20, max_length=500)
    rotation_priority: int = Field(default=100, ge=1, le=1000)


class AccountTokenResponse(BaseModel):
    id: int
    label: str
    token_preview: str
    is_active: bool
    health_status: str
    rotation_priority: int
    usage_count: int


class AccountTokenStatusRequest(BaseModel):
    is_active: bool


class ServerConnectionRequest(BaseModel):
    guild_id: str
    guild_name: str
    role: str = Field(pattern='^(source|target)$')
    enabled: bool = True
    research_scope: str = 'educational_replication'


class ServerConnectionResponse(BaseModel):
    id: int
    guild_id: str
    guild_name: str
    role: str
    enabled: bool
    joined_status: str
    research_scope: str


class PatternCaptureRequest(BaseModel):
    source_guild_id: str
    min_messages_per_user: int = Field(default=2, ge=1, le=1000)
    max_patterns: int = Field(default=20, ge=1, le=200)


class ReplicationStartRequest(BaseModel):
    source_guild_id: str
    target_guild_id: str
    turn_count: int = Field(default=8, ge=1, le=100)
    context_tag_trigger: str = '@'
    educational_mode_confirmed: bool = False


class ReplicationResponse(BaseModel):
    session_id: int
    status: str
    generated_messages: list[dict]


class ChannelMappingRequest(BaseModel):
    source_guild_id: str
    source_channel_id: str
    target_guild_id: str
    target_channel_id: str
    enabled: bool = True
    filters: dict = Field(default_factory=dict)
    settings: dict = Field(default_factory=dict)


class ChannelMappingResponse(BaseModel):
    id: int
    source_guild_id: str
    source_channel_id: str
    target_guild_id: str
    target_channel_id: str
    enabled: bool
    filters: dict
    settings: dict


class ReplicationQueueResponse(BaseModel):
    id: int
    session_id: int
    source_channel_id: str
    target_channel_id: str
    status: str
    attempts: int
    error: str | None = None


class ReplicationControlRequest(BaseModel):
    source_guild_id: str
    target_guild_id: str
    source_channel_id: str
    target_channel_id: str
    source_content: str = Field(min_length=1, max_length=4000)
    source_author_hash: str = Field(min_length=6, max_length=128)


class SystemStatusResponse(BaseModel):
    active_tokens: int
    healthy_tokens: int
    source_connections: int
    target_connections: int
    enabled_channel_mappings: int
    queue_pending: int
    queue_failed: int
    sessions_completed: int
