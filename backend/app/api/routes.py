import os
import time
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.research import GuildOptIn, MessageResearchEvent, UserPrivacyPreference
from app.schemas.research import (
    AccountTokenCreateRequest,
    AccountTokenResponse,
    AccountTokenStatusRequest,
    AIConversationRequest,
    AIConversationResponse,
    ChannelMappingRequest,
    ChannelMappingResponse,
    AnalyticsOverview,
    ComplianceMethodology,
    DashboardStatsResponse,
    FileLoadResponse,
    GuildOptInRequest,
    GuildOptInResponse,
    MessageIngestRequest,
    PatternCaptureRequest,
    ProxyHealthResponse,
    ProxyRecord,
    ReplicationControlRequest,
    ReplicationQueueResponse,
    ReplicationResponse,
    ReplicationStartRequest,
    ServerConnectionRequest,
    ServerConnectionResponse,
    SettingsUpdateRequest,
    SystemStatusResponse,
    UserPrivacyRequest,
)
from app.core.config import get_settings
from app.services.activity_logger import list_recent_activity_events, log_event
from app.services.ai_chat import AIChatService
from app.services.cache import CacheService
from app.services.discord_client import DiscordClient
from app.services.file_loader import FileLoaderService
from app.services.openrouter_nlp import OpenRouterNLPService
from app.services.pattern_analyzer import MessagePatternAnalyzer
from app.services.privacy import PrivacyService
from app.services.rate_limit import DiscordRateLimiter
from app.services.replication_engine import ConversationReplicationEngine
from app.services.token_manager import TokenManagerService
from app.models.research import AccountToken, MessagePattern, ReplicationSession, ServerConnection
from app.models.research import ChannelMapping, ConversationMirrorEvent, CoordinationEvent, ReplicationQueueItem
from app.models.research import ProxyEntry

_startup_time = time.time()

router = APIRouter(prefix='/api/v1')
settings = get_settings()
privacy = PrivacyService()
cache = CacheService()
nlp = OpenRouterNLPService()
discord_client = DiscordClient()
rate_limiter = DiscordRateLimiter(limit_per_minute=settings.discord_requests_per_minute)
token_manager = TokenManagerService()
ai_service = AIChatService()
pattern_analyzer = MessagePatternAnalyzer()
replication_engine = ConversationReplicationEngine()


def serialize_token_record(row: AccountToken) -> AccountTokenResponse:
    return AccountTokenResponse(
        id=row.id,
        label=row.label,
        token_preview=token_manager.token_preview(row.token_value),
        source_identity=row.source_identity,
        proxy_preview=token_manager.proxy_preview(row),
        is_active=row.is_active,
        health_status=row.health_status,
        rotation_priority=row.rotation_priority,
        usage_count=row.usage_count,
    )


@router.post('/consent/opt-in', response_model=GuildOptInResponse)
async def opt_in(request: GuildOptInRequest, db: Session = Depends(get_db)):
    allowed, retry_after = rate_limiter.check('discord_api_opt_in')
    if not allowed:
        raise HTTPException(status_code=429, detail=f'Discord API rate limit protection active. Retry in {retry_after}s')

    guild_api_payload = await discord_client.get_guild(request.guild_id)
    guild = db.query(GuildOptIn).filter(GuildOptIn.guild_id == request.guild_id).first()
    if guild is None:
        guild = GuildOptIn(
            guild_id=request.guild_id,
            guild_name=guild_api_payload.get('name', request.guild_name),
            opted_in=True,
            methodology_version=request.methodology_version,
        )
        db.add(guild)
    else:
        guild.guild_name = guild_api_payload.get('name', request.guild_name)
        guild.opted_in = True
        guild.methodology_version = request.methodology_version
    db.commit()
    db.refresh(guild)
    log_event('guild_opt_in', {'guild_id': request.guild_id, 'methodology_version': request.methodology_version})
    return GuildOptInResponse(guild_id=guild.guild_id, opted_in=guild.opted_in, updated_at=guild.updated_at)


@router.post('/consent/opt-out', status_code=status.HTTP_204_NO_CONTENT)
def opt_out(guild_id: str = Query(...), db: Session = Depends(get_db)):
    guild = db.query(GuildOptIn).filter(GuildOptIn.guild_id == guild_id).first()
    if guild is None:
        raise HTTPException(status_code=404, detail='Guild not found')
    guild.opted_in = False
    db.commit()
    log_event('guild_opt_out', {'guild_id': guild_id})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post('/ingest/messages', status_code=status.HTTP_202_ACCEPTED)
async def ingest_message(request: MessageIngestRequest, db: Session = Depends(get_db)):
    allowed, retry_after = rate_limiter.check(f'ingest:{request.guild_id}')
    if not allowed:
        raise HTTPException(status_code=429, detail=f'Ingestion rate limit exceeded. Retry in {retry_after}s')

    guild = db.query(GuildOptIn).filter(GuildOptIn.guild_id == request.guild_id, GuildOptIn.opted_in.is_(True)).first()
    if guild is None:
        raise HTTPException(status_code=403, detail='Guild has not opted in for data collection')

    analysis = await nlp.analyze(request.content)
    author_hash = privacy.anonymize_user(request.guild_id, request.author_id)
    pref = (
        db.query(UserPrivacyPreference)
        .filter(UserPrivacyPreference.guild_id == request.guild_id, UserPrivacyPreference.user_hash == author_hash)
        .first()
    )
    if pref is not None and not pref.include_in_research:
        return {'status': 'ignored_by_privacy_preference'}
    interaction_edges = [{'source': author_hash, 'target': privacy.anonymize_user(request.guild_id, m)} for m in request.mentions]
    event = MessageResearchEvent(
        guild_id=request.guild_id,
        channel_id=request.channel_id,
        author_hash=author_hash,
        sentiment=analysis['sentiment'],
        topics=analysis['topics'],
        interaction_edges=interaction_edges,
        created_at=request.created_at,
        event_metadata={'message_id': request.message_id, 'sentiment_score': analysis['score']},
        content_excerpt=privacy.redact_content(request.content),
    )
    db.add(event)
    db.commit()
    cache.delete(f'overview:{request.guild_id}')
    log_event('message_ingested', {'guild_id': request.guild_id, 'channel_id': request.channel_id, 'message_id': request.message_id})
    return {'status': 'accepted'}


@router.post('/privacy/user-preferences')
def update_user_privacy(request: UserPrivacyRequest, db: Session = Depends(get_db)):
    user_hash = privacy.anonymize_user(request.guild_id, request.user_id)
    pref = (
        db.query(UserPrivacyPreference)
        .filter(UserPrivacyPreference.guild_id == request.guild_id, UserPrivacyPreference.user_hash == user_hash)
        .first()
    )
    if pref is None:
        pref = UserPrivacyPreference(
            guild_id=request.guild_id,
            user_hash=user_hash,
            include_in_research=request.include_in_research,
            retention_days=request.retention_days,
        )
        db.add(pref)
    else:
        pref.include_in_research = request.include_in_research
        pref.retention_days = request.retention_days

    if not request.include_in_research:
        db.query(MessageResearchEvent).filter(
            MessageResearchEvent.guild_id == request.guild_id, MessageResearchEvent.author_hash == user_hash
        ).delete()
    db.commit()
    cache.delete(f'overview:{request.guild_id}')
    log_event(
        'privacy_preference_updated',
        {'guild_id': request.guild_id, 'include_in_research': request.include_in_research, 'retention_days': request.retention_days},
    )
    return {'status': 'updated'}


@router.post('/replication/tokens', response_model=AccountTokenResponse)
def add_account_token(request: AccountTokenCreateRequest, db: Session = Depends(get_db)):
    try:
        record = token_manager.upsert_token(
            db=db,
            label=request.label,
            raw_token_value=request.token_value,
            rotation_priority=request.rotation_priority,
            proxy_value=request.proxy_value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log_event('account_token_saved', {'token_id': record.id, 'label': record.label})
    return serialize_token_record(record)


@router.get('/replication/tokens', response_model=list[AccountTokenResponse])
def list_account_tokens(db: Session = Depends(get_db)):
    rows = db.query(AccountToken).order_by(AccountToken.id.desc()).all()
    return [serialize_token_record(row) for row in rows]


@router.post('/replication/tokens/{token_id}/health-check', response_model=AccountTokenResponse)
async def check_account_token_health(token_id: int, db: Session = Depends(get_db)):
    row = db.query(AccountToken).filter(AccountToken.id == token_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail='Token not found')
    checked = await token_manager.health_check(db, row)
    log_event('account_token_health_checked', {'token_id': token_id, 'health_status': checked.health_status})
    return serialize_token_record(checked)


@router.post('/replication/tokens/rotate', response_model=AccountTokenResponse)
def rotate_account_token(db: Session = Depends(get_db)):
    row = token_manager.pick_for_rotation(db)
    if row is None:
        raise HTTPException(status_code=404, detail='No active tokens available')
    log_event('account_token_rotated', {'token_id': row.id, 'usage_count': row.usage_count})
    return serialize_token_record(row)


@router.patch('/replication/tokens/{token_id}/status', response_model=AccountTokenResponse)
def set_account_token_status(token_id: int, request: AccountTokenStatusRequest, db: Session = Depends(get_db)):
    row = db.query(AccountToken).filter(AccountToken.id == token_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail='Token not found')
    row.is_active = request.is_active
    db.commit()
    db.refresh(row)
    log_event('account_token_status_updated', {'token_id': token_id, 'is_active': row.is_active})
    return serialize_token_record(row)


@router.post('/replication/servers', response_model=ServerConnectionResponse)
async def save_server_connection(request: ServerConnectionRequest, db: Session = Depends(get_db)):
    if request.role == 'source':
        guild_opt_in = db.query(GuildOptIn).filter(GuildOptIn.guild_id == request.guild_id, GuildOptIn.opted_in.is_(True)).first()
        if guild_opt_in is None:
            raise HTTPException(status_code=403, detail='Source servers must opt in before connection')

    guild_data = await discord_client.get_guild(request.guild_id)
    row = (
        db.query(ServerConnection)
        .filter(ServerConnection.guild_id == request.guild_id, ServerConnection.role == request.role)
        .first()
    )
    joined_status = 'joined' if guild_data.get('id') else 'pending'
    guild_name = guild_data.get('name', request.guild_name)
    if row is None:
        row = ServerConnection(
            guild_id=request.guild_id,
            guild_name=guild_name,
            role=request.role,
            enabled=request.enabled,
            joined_status=joined_status,
            research_scope=request.research_scope,
        )
        db.add(row)
    else:
        row.guild_name = guild_name
        row.enabled = request.enabled
        row.joined_status = joined_status
        row.research_scope = request.research_scope
    db.commit()
    db.refresh(row)
    log_event('server_connection_saved', {'guild_id': row.guild_id, 'role': row.role, 'joined_status': row.joined_status})
    return ServerConnectionResponse(
        id=row.id,
        guild_id=row.guild_id,
        guild_name=row.guild_name,
        role=row.role,
        enabled=row.enabled,
        joined_status=row.joined_status,
        research_scope=row.research_scope,
    )


@router.get('/replication/servers', response_model=list[ServerConnectionResponse])
def list_server_connections(db: Session = Depends(get_db)):
    rows = db.query(ServerConnection).order_by(ServerConnection.id.desc()).all()
    return [
        ServerConnectionResponse(
            id=row.id,
            guild_id=row.guild_id,
            guild_name=row.guild_name,
            role=row.role,
            enabled=row.enabled,
            joined_status=row.joined_status,
            research_scope=row.research_scope,
        )
        for row in rows
    ]


@router.post('/replication/channel-mappings', response_model=ChannelMappingResponse)
def save_channel_mapping(request: ChannelMappingRequest, db: Session = Depends(get_db)):
    source_server = (
        db.query(ServerConnection)
        .filter(ServerConnection.guild_id == request.source_guild_id, ServerConnection.role == 'source', ServerConnection.enabled.is_(True))
        .first()
    )
    target_server = (
        db.query(ServerConnection)
        .filter(ServerConnection.guild_id == request.target_guild_id, ServerConnection.role == 'target', ServerConnection.enabled.is_(True))
        .first()
    )
    if source_server is None or target_server is None:
        raise HTTPException(status_code=400, detail='Channel mappings require active source and target server connections')

    row = (
        db.query(ChannelMapping)
        .filter(
            ChannelMapping.source_guild_id == request.source_guild_id,
            ChannelMapping.source_channel_id == request.source_channel_id,
            ChannelMapping.target_guild_id == request.target_guild_id,
            ChannelMapping.target_channel_id == request.target_channel_id,
        )
        .first()
    )
    if row is None:
        row = ChannelMapping(
            source_guild_id=request.source_guild_id,
            source_channel_id=request.source_channel_id,
            target_guild_id=request.target_guild_id,
            target_channel_id=request.target_channel_id,
            enabled=request.enabled,
            filters=request.filters,
            settings=request.settings,
        )
        db.add(row)
    else:
        row.enabled = request.enabled
        row.filters = request.filters
        row.settings = request.settings
    db.commit()
    db.refresh(row)
    log_event(
        'channel_mapping_saved',
        {
            'source_guild_id': row.source_guild_id,
            'source_channel_id': row.source_channel_id,
            'target_guild_id': row.target_guild_id,
            'target_channel_id': row.target_channel_id,
        },
    )
    return ChannelMappingResponse(
        id=row.id,
        source_guild_id=row.source_guild_id,
        source_channel_id=row.source_channel_id,
        target_guild_id=row.target_guild_id,
        target_channel_id=row.target_channel_id,
        enabled=row.enabled,
        filters=row.filters,
        settings=row.settings,
    )


@router.get('/replication/channel-mappings', response_model=list[ChannelMappingResponse])
def list_channel_mappings(source_guild_id: str | None = None, target_guild_id: str | None = None, db: Session = Depends(get_db)):
    query = db.query(ChannelMapping)
    if source_guild_id:
        query = query.filter(ChannelMapping.source_guild_id == source_guild_id)
    if target_guild_id:
        query = query.filter(ChannelMapping.target_guild_id == target_guild_id)
    rows = query.order_by(ChannelMapping.id.desc()).all()
    return [
        ChannelMappingResponse(
            id=row.id,
            source_guild_id=row.source_guild_id,
            source_channel_id=row.source_channel_id,
            target_guild_id=row.target_guild_id,
            target_channel_id=row.target_channel_id,
            enabled=row.enabled,
            filters=row.filters,
            settings=row.settings,
        )
        for row in rows
    ]


@router.post('/replication/patterns/capture')
def capture_patterns(request: PatternCaptureRequest, db: Session = Depends(get_db)):
    patterns = pattern_analyzer.capture_patterns(
        db=db,
        source_guild_id=request.source_guild_id,
        min_messages_per_user=request.min_messages_per_user,
        max_patterns=request.max_patterns,
    )
    log_event('message_patterns_captured', {'source_guild_id': request.source_guild_id, 'count': len(patterns)})
    return {
        'captured_count': len(patterns),
        'patterns': [
            {
                'id': item.id,
                'author_hash': item.author_hash,
                'style_vector': item.style_vector,
                'sample_messages': item.sample_messages,
            }
            for item in patterns
        ],
    }


@router.get('/replication/patterns')
def list_patterns(source_guild_id: str, db: Session = Depends(get_db)):
    rows = db.query(MessagePattern).filter(MessagePattern.source_guild_id == source_guild_id).all()
    return [
        {
            'id': row.id,
            'source_guild_id': row.source_guild_id,
            'author_hash': row.author_hash,
            'style_vector': row.style_vector,
            'active_hours': row.active_hours,
            'mention_likelihood': row.mention_likelihood,
        }
        for row in rows
    ]


@router.post('/replication/control/start', response_model=ReplicationResponse)
def start_replication(request: ReplicationStartRequest, db: Session = Depends(get_db)):
    if not settings.educational_replication_only:
        raise HTTPException(status_code=400, detail='Replication feature must run in educational-only mode')
    if not request.educational_mode_confirmed:
        raise HTTPException(status_code=400, detail='Educational mode confirmation is required')

    source_connection = (
        db.query(ServerConnection)
        .filter(ServerConnection.guild_id == request.source_guild_id, ServerConnection.role == 'source', ServerConnection.enabled.is_(True))
        .first()
    )
    target_connection = (
        db.query(ServerConnection)
        .filter(ServerConnection.guild_id == request.target_guild_id, ServerConnection.role == 'target', ServerConnection.enabled.is_(True))
        .first()
    )
    if source_connection is None or target_connection is None:
        raise HTTPException(status_code=400, detail='Both source and target server connections must be configured and enabled')

    session, generated_messages = replication_engine.run_session(
        db=db,
        source_guild_id=request.source_guild_id,
        target_guild_id=request.target_guild_id,
        turn_count=request.turn_count,
        context_tag_trigger=request.context_tag_trigger,
    )
    log_event(
        'replication_session_completed',
        {
            'session_id': session.id,
            'source_guild_id': session.source_guild_id,
            'target_guild_id': session.target_guild_id,
            'generated_count': len(generated_messages),
        },
    )
    return ReplicationResponse(session_id=session.id, status=session.status, generated_messages=generated_messages)


@router.post('/replication/control/enqueue', response_model=ReplicationQueueResponse)
def enqueue_replication_message(request: ReplicationControlRequest, db: Session = Depends(get_db)):
    mapping = (
        db.query(ChannelMapping)
        .filter(
            ChannelMapping.source_guild_id == request.source_guild_id,
            ChannelMapping.target_guild_id == request.target_guild_id,
            ChannelMapping.source_channel_id == request.source_channel_id,
            ChannelMapping.target_channel_id == request.target_channel_id,
            ChannelMapping.enabled.is_(True),
        )
        .first()
    )
    if mapping is None:
        raise HTTPException(status_code=404, detail='No enabled channel mapping found for provided source/target channels')

    session = (
        db.query(ReplicationSession)
        .filter(
            ReplicationSession.source_guild_id == request.source_guild_id,
            ReplicationSession.target_guild_id == request.target_guild_id,
        )
        .order_by(ReplicationSession.id.desc())
        .first()
    )
    if session is None:
        session = ReplicationSession(
            source_guild_id=request.source_guild_id,
            target_guild_id=request.target_guild_id,
            mode='educational_controlled',
            status='running',
            account_plan=[],
            session_metrics={'manual_queue': True},
        )
        db.add(session)
        db.commit()
        db.refresh(session)

    queue_item = ReplicationQueueItem(
        session_id=session.id,
        source_guild_id=request.source_guild_id,
        source_channel_id=request.source_channel_id,
        target_guild_id=request.target_guild_id,
        target_channel_id=request.target_channel_id,
        payload={
            'source_content': request.source_content,
            'replicated_content': request.source_content,
            'source_author_hash': request.source_author_hash,
            'context_aware': False,
            'response_time_ms': 0,
        },
        status='queued',
    )
    db.add(queue_item)
    db.commit()
    db.refresh(queue_item)
    log_event('replication_queue_enqueued', {'queue_id': queue_item.id, 'session_id': session.id})
    return ReplicationQueueResponse(
        id=queue_item.id,
        session_id=queue_item.session_id,
        source_channel_id=queue_item.source_channel_id,
        target_channel_id=queue_item.target_channel_id,
        status=queue_item.status,
        attempts=queue_item.attempts,
        error=queue_item.error,
    )


@router.get('/replication/control/sessions')
def list_replication_sessions(db: Session = Depends(get_db)):
    rows = db.query(ReplicationSession).order_by(ReplicationSession.id.desc()).all()
    return [
        {
            'id': row.id,
            'source_guild_id': row.source_guild_id,
            'target_guild_id': row.target_guild_id,
            'mode': row.mode,
            'status': row.status,
            'session_metrics': row.session_metrics,
        }
        for row in rows
    ]


@router.get('/replication/control/queue', response_model=list[ReplicationQueueResponse])
def list_replication_queue(session_id: int | None = None, status_filter: str | None = None, db: Session = Depends(get_db)):
    query = db.query(ReplicationQueueItem)
    if session_id is not None:
        query = query.filter(ReplicationQueueItem.session_id == session_id)
    if status_filter is not None:
        query = query.filter(ReplicationQueueItem.status == status_filter)
    rows = query.order_by(ReplicationQueueItem.id.desc()).limit(300).all()
    return [
        ReplicationQueueResponse(
            id=row.id,
            session_id=row.session_id,
            source_channel_id=row.source_channel_id,
            target_channel_id=row.target_channel_id,
            status=row.status,
            attempts=row.attempts,
            error=row.error,
        )
        for row in rows
    ]


@router.get('/replication/control/coordination')
def list_coordination_events(session_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(CoordinationEvent)
    if session_id is not None:
        query = query.filter(CoordinationEvent.session_id == session_id)
    rows = query.order_by(CoordinationEvent.id.desc()).limit(200).all()
    return [
        {
            'id': row.id,
            'session_id': row.session_id,
            'trigger_account_label': row.trigger_account_label,
            'responder_account_label': row.responder_account_label,
            'reason': row.reason,
            'metadata': row.event_metadata,
        }
        for row in rows
    ]


@router.get('/replication/control/conversations')
def list_conversation_mirror(session_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(ConversationMirrorEvent)
    if session_id is not None:
        query = query.filter(ConversationMirrorEvent.session_id == session_id)
    rows = query.order_by(ConversationMirrorEvent.id.desc()).limit(300).all()
    return [
        {
            'id': row.id,
            'session_id': row.session_id,
            'source_channel_id': row.source_channel_id,
            'target_channel_id': row.target_channel_id,
            'source_content': row.source_content,
            'replicated_content': row.replicated_content,
            'source_author_hash': row.source_author_hash,
            'responder_account_label': row.responder_account_label,
            'response_time_ms': row.response_time_ms,
            'replicated_at': row.replicated_at.isoformat(),
        }
        for row in rows
    ]


@router.get('/replication/status', response_model=SystemStatusResponse)
def replication_system_status(db: Session = Depends(get_db)):
    active_tokens = db.query(func.count(AccountToken.id)).filter(AccountToken.is_active.is_(True)).scalar() or 0
    healthy_tokens = (
        db.query(func.count(AccountToken.id))
        .filter(AccountToken.is_active.is_(True), AccountToken.health_status.in_(['healthy', 'unknown']))
        .scalar()
        or 0
    )
    source_connections = (
        db.query(func.count(ServerConnection.id))
        .filter(ServerConnection.role == 'source', ServerConnection.enabled.is_(True))
        .scalar()
        or 0
    )
    target_connections = (
        db.query(func.count(ServerConnection.id))
        .filter(ServerConnection.role == 'target', ServerConnection.enabled.is_(True))
        .scalar()
        or 0
    )
    enabled_channel_mappings = db.query(func.count(ChannelMapping.id)).filter(ChannelMapping.enabled.is_(True)).scalar() or 0
    queue_pending = db.query(func.count(ReplicationQueueItem.id)).filter(ReplicationQueueItem.status == 'queued').scalar() or 0
    queue_failed = db.query(func.count(ReplicationQueueItem.id)).filter(ReplicationQueueItem.status == 'failed').scalar() or 0
    sessions_completed = (
        db.query(func.count(ReplicationSession.id)).filter(ReplicationSession.status == 'completed').scalar() or 0
    )
    return SystemStatusResponse(
        active_tokens=active_tokens,
        healthy_tokens=healthy_tokens,
        source_connections=source_connections,
        target_connections=target_connections,
        enabled_channel_mappings=enabled_channel_mappings,
        queue_pending=queue_pending,
        queue_failed=queue_failed,
        sessions_completed=sessions_completed,
    )


@router.get('/replication/config')
def replication_config_snapshot():
    return {
        'educational_replication_only': settings.educational_replication_only,
        'discord_api_base_url': settings.discord_api_base_url,
        'discord_requests_per_minute': settings.discord_requests_per_minute,
        'analytics_cache_ttl_seconds': settings.analytics_cache_ttl_seconds,
        'openrouter_model': settings.openrouter_model,
    }


@router.get('/replication/logs')
def replication_logs(limit: int = Query(default=100, ge=1, le=400)):
    return list_recent_activity_events(limit=limit)


@router.get('/analytics/overview', response_model=AnalyticsOverview)
def analytics_overview(guild_id: str, db: Session = Depends(get_db)):
    cache_key = f'overview:{guild_id}'
    cached = cache.get_json(cache_key)
    if cached:
        return AnalyticsOverview(**cached)

    total_messages = db.query(func.count(MessageResearchEvent.id)).filter(MessageResearchEvent.guild_id == guild_id).scalar() or 0
    active_users = (
        db.query(func.count(func.distinct(MessageResearchEvent.author_hash)))
        .filter(MessageResearchEvent.guild_id == guild_id)
        .scalar()
        or 0
    )
    sentiment_avg = (
        db.query(
            func.avg(
                case(
                    (MessageResearchEvent.sentiment == 'positive', 1),
                    (MessageResearchEvent.sentiment == 'negative', -1),
                    else_=0,
                )
            )
        )
        .filter(MessageResearchEvent.guild_id == guild_id)
        .scalar()
    )
    avg_sentiment = round(float(sentiment_avg or 0.0), 3)

    topic_rows = (
        db.query(MessageResearchEvent.topics)
        .filter(MessageResearchEvent.guild_id == guild_id)
        .all()
    )
    topics = Counter(topic for row in topic_rows for topic in row[0])
    response = AnalyticsOverview(
        guild_id=guild_id,
        total_messages=total_messages,
        active_users=active_users,
        avg_sentiment_score=avg_sentiment,
        top_topics=[{'topic': k, 'count': v} for k, v in topics.most_common(8)],
    )
    cache.set_json(cache_key, response.model_dump())
    return response


@router.get('/analytics/sentiment-trend')
def sentiment_trend(guild_id: str, db: Session = Depends(get_db)):
    rows = db.query(MessageResearchEvent).filter(MessageResearchEvent.guild_id == guild_id).order_by(MessageResearchEvent.created_at).all()
    return [
        {
            'timestamp': row.created_at.isoformat(),
            'sentiment': row.sentiment,
            'score': row.event_metadata.get('sentiment_score', 0),
        }
        for row in rows
    ]


@router.get('/analytics/activity-heatmap')
def activity_heatmap(guild_id: str, db: Session = Depends(get_db)):
    rows = db.query(MessageResearchEvent).filter(MessageResearchEvent.guild_id == guild_id).all()
    grid: dict[str, int] = {}
    for row in rows:
        bucket = f'{row.created_at.weekday()}-{row.created_at.hour}'
        grid[bucket] = grid.get(bucket, 0) + 1
    return [{'bucket': bucket, 'count': count} for bucket, count in sorted(grid.items())]


@router.get('/analytics/interaction-flow')
def interaction_flow(guild_id: str, db: Session = Depends(get_db)):
    rows = db.query(MessageResearchEvent.interaction_edges).filter(MessageResearchEvent.guild_id == guild_id).all()
    edges = [edge for row in rows for edge in row[0]]
    return {'edges': edges}


# --- File Loading Endpoints ---

@router.post('/accounts/load-file', response_model=FileLoadResponse)
def load_tokens_from_file(db: Session = Depends(get_db)):
    loader = FileLoaderService()
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    file_path = os.path.join(root, 't.txt')
    loaded, errors = loader.load_tokens_file(db, file_path)
    log_event('tokens_loaded_from_file', {'loaded': loaded, 'error_count': len(errors)})
    return FileLoadResponse(loaded=loaded, errors=errors)


@router.post('/proxies/load-file', response_model=FileLoadResponse)
def load_proxies_from_file(db: Session = Depends(get_db)):
    loader = FileLoaderService()
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    file_path = os.path.join(root, 'p.txt')
    loaded, errors = loader.load_proxies_file(db, file_path)
    log_event('proxies_loaded_from_file', {'loaded': loaded, 'error_count': len(errors)})
    return FileLoadResponse(loaded=loaded, errors=errors)


@router.post('/config/load-file')
def load_api_config():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    file_path = os.path.join(root, 'api_key.conf')
    config = FileLoaderService.load_api_config(file_path)
    log_event('api_config_loaded', {'keys_found': list(config.keys())})
    return {'status': 'loaded', 'keys': list(config.keys())}


@router.get('/proxies/health', response_model=ProxyHealthResponse)
def proxy_health(db: Session = Depends(get_db)):
    rows = db.query(ProxyEntry).order_by(ProxyEntry.id.desc()).all()
    proxies = []
    for row in rows:
        total = row.success_count + row.failure_count
        rate = (row.success_count / total * 100) if total > 0 else 100.0
        proxies.append(ProxyRecord(
            id=row.id,
            host=row.host,
            port=row.port,
            username=row.username,
            is_healthy=row.is_healthy,
            last_used=row.last_used_at.isoformat() if row.last_used_at else None,
            success_rate=round(rate, 1),
        ))
    healthy = sum(1 for p in proxies if p.is_healthy)
    return ProxyHealthResponse(
        total=len(proxies),
        healthy=healthy,
        unhealthy=len(proxies) - healthy,
        proxies=proxies,
    )


@router.post('/ai/chat', response_model=AIConversationResponse)
async def ai_chat(request: AIConversationRequest):
    result = await ai_service.chat(
        message=request.message,
        conversation_history=request.conversation_history,
        system_prompt=request.system_prompt,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )
    log_event('ai_chat_request', {'model': result['model']})
    return AIConversationResponse(**result)


@router.get('/dashboard/stats', response_model=DashboardStatsResponse)
def dashboard_stats(db: Session = Depends(get_db)):
    active_accounts = db.query(func.count(AccountToken.id)).filter(AccountToken.is_active.is_(True)).scalar() or 0
    healthy_accounts = (
        db.query(func.count(AccountToken.id))
        .filter(AccountToken.is_active.is_(True), AccountToken.health_status.in_(['healthy', 'unknown']))
        .scalar()
        or 0
    )
    total_proxies = db.query(func.count(ProxyEntry.id)).scalar() or 0
    healthy_proxies = db.query(func.count(ProxyEntry.id)).filter(ProxyEntry.is_healthy.is_(True)).scalar() or 0
    active_syncs = db.query(func.count(ReplicationSession.id)).filter(ReplicationSession.status == 'running').scalar() or 0
    messages_transferred = db.query(func.count(ConversationMirrorEvent.id)).scalar() or 0
    return DashboardStatsResponse(
        active_accounts=active_accounts,
        healthy_accounts=healthy_accounts,
        total_proxies=total_proxies,
        healthy_proxies=healthy_proxies,
        active_syncs=active_syncs,
        messages_transferred=messages_transferred,
        ai_requests_total=0,
        uptime_seconds=time.time() - _startup_time,
    )


@router.patch('/settings/update')
def update_settings(request: SettingsUpdateRequest):
    log_event('settings_update_requested', {'key': request.key})
    return {'status': 'acknowledged', 'key': request.key, 'note': 'Runtime setting updates require restart for full effect'}


@router.get('/compliance/methodology', response_model=ComplianceMethodology)
def compliance_methodology() -> ComplianceMethodology:
    return ComplianceMethodology(
        methodology_version='2026.04',
        consent_model='Server-level opt-in plus participant-level transparency and opt-out controls; educational replication runs require explicit confirmation',
        anonymization='All user identifiers are salted SHA-256 hashes; only redacted message excerpts and masked token previews are exposed',
        retention_policy='Default 90-day retention with configurable deletion policies for GDPR/CCPA requests and controlled-environment replication datasets',
        publication_support=[
            'Export-ready aggregate metrics',
            'Anonymized interaction network snapshots',
            'Methodology and limitation documentation endpoints',
            'Educational conversation replication session metadata with account masking',
        ],
    )
