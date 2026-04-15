from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.research import GuildOptIn, MessageResearchEvent, UserPrivacyPreference
from app.schemas.research import (
    AnalyticsOverview,
    ComplianceMethodology,
    GuildOptInRequest,
    GuildOptInResponse,
    MessageIngestRequest,
    UserPrivacyRequest,
)
from app.core.config import get_settings
from app.services.activity_logger import log_event
from app.services.cache import CacheService
from app.services.discord_client import DiscordClient
from app.services.openrouter_nlp import OpenRouterNLPService
from app.services.privacy import PrivacyService
from app.services.rate_limit import DiscordRateLimiter

router = APIRouter(prefix='/api/v1')
settings = get_settings()
privacy = PrivacyService()
cache = CacheService()
nlp = OpenRouterNLPService()
discord_client = DiscordClient()
rate_limiter = DiscordRateLimiter(limit_per_minute=settings.discord_requests_per_minute)


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


@router.get('/compliance/methodology', response_model=ComplianceMethodology)
def compliance_methodology() -> ComplianceMethodology:
    return ComplianceMethodology(
        methodology_version='2026.04',
        consent_model='Server-level opt-in plus participant-level transparency and opt-out controls',
        anonymization='All user identifiers are salted SHA-256 hashes; only redacted message excerpts are stored',
        retention_policy='Default 90-day retention with configurable deletion policies for GDPR/CCPA requests',
        publication_support=[
            'Export-ready aggregate metrics',
            'Anonymized interaction network snapshots',
            'Methodology and limitation documentation endpoints',
        ],
    )
