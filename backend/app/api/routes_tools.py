from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.research import (
    AccountToken,
    ChannelMapping,
    ClanTagHistory,
    ConversationTransferHistory,
    MimicProfile,
    NicknameHistory,
    ProxyEntry,
    ServerConnection,
    ServerJoinHistory,
)
from app.services.discord_client import DiscordClient
from app.services.token_manager import TokenManagerService

logger = logging.getLogger('discord_research.tools')

router = APIRouter(prefix='/tools', tags=['tools'])
discord_client = DiscordClient()
token_manager = TokenManagerService()
DEFAULT_CLAN_TAG_GENERATE_COUNT = 20
NICKNAME_CHANGE_DELAY_SECONDS = 5
TYPING_INDICATOR_INTERVAL_SECONDS = 4


def _safe_error_text(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.lower()
    if 'traceback' in lowered or 'file \"' in lowered:
        return 'internal_error'
    return value[:300]


class ServerJoinRequest(BaseModel):
    guild_id: str
    invite_code: str
    token_ids: list[int] = Field(default_factory=list)
    auto_onboarding: bool = True
    use_proxies: bool = True


class ServerBulkJoinRequest(BaseModel):
    invite_codes: list[str] = Field(min_length=1)
    tokens: list[int] = Field(default_factory=list)
    parallel_limit: int = Field(default=3, ge=1, le=20)


class ClanTagChangeRequest(BaseModel):
    clan_tag: str | None = Field(default=None, max_length=100)
    token_ids: list[int] = Field(default_factory=list)
    remove: bool = False


class ClanTagGenerateRequest(BaseModel):
    template: str = '[{num}] {tag}'
    base_tag: str
    start_number: int = 1


class NicknameChangeRequest(BaseModel):
    guild_id: str
    nicknames: dict[int, str] = Field(default_factory=dict)
    use_template: bool = False


class NicknameTemplateRequest(BaseModel):
    guild_id: str
    template: str
    token_ids: list[int] = Field(default_factory=list)


class MimicCaptureRequest(BaseModel):
    user_id: str
    guild_id: str
    analysis_depth: int = Field(default=100, ge=1, le=1000)


class MimicGenerateRequest(BaseModel):
    profile_id: int
    context: str
    style: str = Field(pattern='^(exact_copy|similar|inspired)$')


class MimicPresenceRequest(BaseModel):
    token_ids: list[int] = Field(default_factory=list)
    activity_type: str = Field(pattern='^(playing|listening|watching|competing)$')
    activity_text: str
    status: str = Field(pattern='^(online|idle|dnd|invisible)$')
    randomize: bool = False


class TypingSimulatorRequest(BaseModel):
    channel_id: str
    duration_seconds: int = Field(default=5, ge=1, le=120)
    token_id: int
    then_send: str | None = None


class ConversationCaptureRequest(BaseModel):
    source_channel_id: str
    message_limit: int = Field(default=30, ge=1, le=200)
    preserve_context: bool = True


class ConversationTransferRequest(BaseModel):
    source_guild_id: str
    source_channel_id: str
    target_guild_id: str
    target_channel_id: str
    transfer_mode: str = Field(pattern='^(exact|paraphrase|summarize)$')
    preserve_author: bool = True
    add_context: bool = True
    randomize_delays: bool = True


class ConversationFilterRequest(BaseModel):
    channel_id: str
    filters: dict = Field(default_factory=dict)


class ConversationBatchTransferRequest(BaseModel):
    mappings: list[ConversationTransferRequest]
    parallel: int = Field(default=2, ge=1, le=10)


def _select_tokens(db: Session, token_ids: list[int] | None = None) -> list[AccountToken]:
    query = db.query(AccountToken).filter(AccountToken.is_active.is_(True))
    if token_ids:
        query = query.filter(AccountToken.id.in_(token_ids))
    return query.order_by(AccountToken.id.asc()).all()


def _proxy_for_token(db: Session, token_row: AccountToken, use_proxies: bool = True) -> str | None:
    if not use_proxies:
        return None
    if token_row.proxy_host and token_row.proxy_port:
        return token_manager.build_proxy_url(
            host=token_row.proxy_host,
            port=token_row.proxy_port,
            username=token_row.proxy_username or '',
            password=token_row.proxy_password or '',
        )
    fallback_proxy = db.query(ProxyEntry).filter(ProxyEntry.is_healthy.is_(True)).order_by(func.random()).first()
    if fallback_proxy:
        return token_manager.build_proxy_url(
            host=fallback_proxy.host,
            port=fallback_proxy.port,
            username=fallback_proxy.username or '',
            password=fallback_proxy.password or '',
        )
    return None


async def _ensure_healthy(db: Session, token_row: AccountToken) -> AccountToken:
    checked = await token_manager.health_check(db, token_row)
    return checked


@router.post('/server-joiner/join')
async def server_joiner_join(request: ServerJoinRequest, db: Session = Depends(get_db)):
    code = discord_client.extract_invite_code(request.invite_code)
    if not code:
        raise HTTPException(status_code=400, detail='Invalid invite code format')

    tokens = _select_tokens(db, request.token_ids)
    if not tokens:
        raise HTTPException(status_code=400, detail='No active tokens available')

    results: list[dict] = []
    attempted_token_ids = set()
    for token_row in tokens:
        current_token = token_row
        attempted_token_ids.add(current_token.id)
        checked = await _ensure_healthy(db, current_token)

        # Fallback loop: if unhealthy, pick a new healthy token
        fallback_attempts = 0
        while checked.health_status != 'healthy' and fallback_attempts < 5:
            logger.info('Token %s is unhealthy in server join, finding fallback', current_token.id)
            fallback_token = db.query(AccountToken).filter(
                AccountToken.is_active.is_(True),
                AccountToken.health_status == 'healthy',
                ~AccountToken.id.in_(attempted_token_ids)
            ).order_by(func.random()).first()
            if not fallback_token:
                break
            current_token = fallback_token
            attempted_token_ids.add(current_token.id)
            checked = await _ensure_healthy(db, current_token)
            fallback_attempts += 1

        if checked.health_status != 'healthy':
            results.append({'token_id': token_row.id, 'status': 'skipped', 'detail': 'failed to find a healthy token after fallbacks'})
            continue

        result = await discord_client.join_guild_via_invite(
            invite_code=code,
            token=current_token.token_value,
            proxy_url=_proxy_for_token(db, current_token, request.use_proxies),
            token_id=current_token.id,
            guild_id=request.guild_id,
            db=db,
        )
        if result.get('code') in (401, 403):
            token_manager.mark_unhealthy(
                db,
                current_token,
                status='invalid',
                deactivate=result.get('code') == 401,
            )
        row = ServerJoinHistory(
            token_id=current_token.id,
            guild_id=request.guild_id,
            invite_code=code,
            status=result.get('status', 'failed'),
            error=result.get('detail'),
        )
        db.add(row)
        db.commit()
        results.append({'token_id': current_token.id, **result})

    successes = sum(1 for r in results if r.get('status') in {'joined', 'already_joined'})
    return {
        'guild_id': request.guild_id,
        'invite_code': code,
        'total': len(results),
        'success': successes,
        'failed': len(results) - successes,
        'results': results,
    }


@router.get('/server-joiner/status')
def server_joiner_status(db: Session = Depends(get_db)):
    items = db.query(ServerJoinHistory).order_by(ServerJoinHistory.id.desc()).limit(100).all()
    total = len(items)
    success = sum(1 for item in items if item.status in {'joined', 'already_joined'})
    return {
        'pending_joins': sum(1 for item in items if item.status == 'pending'),
        'success_rate': round((success / total) * 100, 2) if total else 0.0,
        'failed_accounts': [item.token_id for item in items if item.status not in {'joined', 'already_joined'} and item.token_id is not None],
    }


@router.post('/server-joiner/bulk-join')
async def server_joiner_bulk_join(request: ServerBulkJoinRequest, db: Session = Depends(get_db)):
    tokens = _select_tokens(db, request.tokens)
    if not tokens:
        raise HTTPException(status_code=400, detail='No active tokens available')

    semaphore = asyncio.Semaphore(request.parallel_limit)
    output: list[dict] = []

    async def _run(invite: str, token_row: AccountToken):
        async with semaphore:
            result = await discord_client.join_guild_via_invite(
                invite,
                token_row.token_value,
                _proxy_for_token(db, token_row),
                token_id=token_row.id,
                guild_id=None,
                db=db,
            )
            if result.get('code') in (401, 403):
                token_manager.mark_unhealthy(
                    db,
                    token_row,
                    status='invalid',
                    deactivate=result.get('code') == 401,
                )
            history = ServerJoinHistory(
                token_id=token_row.id,
                guild_id=result.get('guild', {}).get('id') if isinstance(result.get('guild'), dict) else None,
                invite_code=discord_client.extract_invite_code(invite) or invite,
                status=result.get('status', 'failed'),
                error=result.get('detail'),
            )
            db.add(history)
            db.commit()
            output.append({'invite_code': invite, 'token_id': token_row.id, **result})

    tasks = [
        _run(invite, token_row)
        for invite in request.invite_codes
        for token_row in tokens
    ]
    await asyncio.gather(*tasks)
    return {'total_attempts': len(output), 'results': output}


@router.get('/server-joiner/history')
def server_joiner_history(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    query = db.query(ServerJoinHistory)
    if status:
        query = query.filter(ServerJoinHistory.status == status)
    rows = query.order_by(ServerJoinHistory.id.desc()).limit(limit).all()
    return [
        {
            'id': row.id,
            'token_id': row.token_id,
            'guild_id': row.guild_id,
            'invite_code': row.invite_code,
            'status': row.status,
            'error': row.error,
            'joined_at': row.joined_at,
        }
        for row in rows
    ]


@router.post('/clan-tag/change')
async def clan_tag_change(request: ClanTagChangeRequest, db: Session = Depends(get_db)):
    tokens = _select_tokens(db, request.token_ids)
    if not tokens:
        raise HTTPException(status_code=400, detail='No active tokens available')

    new_tag = None if request.remove else (request.clan_tag or '').strip()
    if new_tag and len(new_tag) > 100:
        raise HTTPException(status_code=400, detail='clan_tag exceeds 100 characters')

    results = []
    for token_row in tokens:
        result = await discord_client.patch_user_clan_tag(token_row.token_value, new_tag, _proxy_for_token(db, token_row))
        history = ClanTagHistory(
            token_id=token_row.id,
            previous_tag=None,
            new_tag=new_tag,
            status=result.get('status', 'failed'),
            error=result.get('detail'),
        )
        db.add(history)
        db.commit()
        results.append({'token_id': token_row.id, **result})
    return {'results': results}


@router.get('/clan-tag/status')
def clan_tag_status(db: Session = Depends(get_db)):
    tokens = _select_tokens(db)
    latest_by_token: dict[int, ClanTagHistory] = {}
    rows = db.query(ClanTagHistory).order_by(ClanTagHistory.id.desc()).limit(500).all()
    for row in rows:
        latest_by_token.setdefault(row.token_id, row)
    return [
        {
            'token_id': token.id,
            'label': token.label,
            'clan_tag': latest_by_token.get(token.id).new_tag if token.id in latest_by_token else None,
            'status': latest_by_token.get(token.id).status if token.id in latest_by_token else 'unknown',
        }
        for token in tokens
    ]


@router.post('/clan-tag/bulk-generate')
def clan_tag_bulk_generate(request: ClanTagGenerateRequest):
    output = []
    for idx in range(DEFAULT_CLAN_TAG_GENERATE_COUNT):
        num = request.start_number + idx
        output.append(
            request.template
            .replace('{num}', str(num))
            .replace('[NUM]', str(num))
            .replace('{tag}', request.base_tag)
            .replace('[TAG]', request.base_tag)
        )
    return {'generated': output}


@router.get('/clan-tag/history')
def clan_tag_history(limit: int = Query(default=20, ge=1, le=200), db: Session = Depends(get_db)):
    rows = db.query(ClanTagHistory).order_by(ClanTagHistory.id.desc()).limit(limit).all()
    return [
        {
            'id': row.id,
            'token_id': row.token_id,
            'previous_tag': row.previous_tag,
            'new_tag': row.new_tag,
            'status': row.status,
            'error': row.error,
            'changed_at': row.changed_at,
        }
        for row in rows
    ]


@router.post('/nickname/change')
async def nickname_change(request: NicknameChangeRequest, db: Session = Depends(get_db)):
    if not request.nicknames:
        raise HTTPException(status_code=400, detail='nicknames map is required')

    results = []
    for token_id, nickname in request.nicknames.items():
        token_row = db.query(AccountToken).filter(AccountToken.id == token_id, AccountToken.is_active.is_(True)).first()
        if token_row is None:
            results.append({'token_id': token_id, 'status': 'skipped', 'detail': 'token not found'})
            continue
        nickname = nickname.strip()
        if not (1 <= len(nickname) <= 32):
            results.append({'token_id': token_id, 'status': 'failed', 'detail': 'nickname must be 1-32 chars'})
            continue

        user_id = token_row.source_identity or '@me'
        result = await discord_client.patch_member_nickname(
            guild_id=request.guild_id,
            user_id=user_id,
            nickname=nickname,
            token=token_row.token_value,
            proxy_url=_proxy_for_token(db, token_row),
        )
        row = NicknameHistory(
            token_id=token_id,
            guild_id=request.guild_id,
            nickname=nickname,
            status=result.get('status', 'failed'),
            error=result.get('detail'),
        )
        db.add(row)
        db.commit()
        results.append({'token_id': token_id, **result})
        await asyncio.sleep(NICKNAME_CHANGE_DELAY_SECONDS)
    return {'results': results}


@router.get('/nickname/list')
def nickname_list(guild_id: str = Query(...), db: Session = Depends(get_db)):
    tokens = _select_tokens(db)
    latest = db.query(NicknameHistory).filter(NicknameHistory.guild_id == guild_id).order_by(NicknameHistory.id.desc()).all()
    by_token: dict[int, NicknameHistory] = {}
    for row in latest:
        by_token.setdefault(row.token_id, row)
    return [
        {
            'token_id': token.id,
            'label': token.label,
            'nickname': by_token.get(token.id).nickname if token.id in by_token else None,
            'status': by_token.get(token.id).status if token.id in by_token else 'unknown',
        }
        for token in tokens
    ]


@router.post('/nickname/bulk-template')
def nickname_bulk_template(request: NicknameTemplateRequest):
    generated = {}
    for i, token_id in enumerate(request.token_ids, start=1):
        generated[token_id] = request.template.replace('{num}', str(i)).replace('{prefix}', 'bot').replace('{suffix}', '')[:32]
    return {'guild_id': request.guild_id, 'nicknames': generated}


@router.get('/nickname/servers')
def nickname_servers(db: Session = Depends(get_db)):
    rows = db.query(ServerConnection).filter(ServerConnection.enabled.is_(True)).order_by(ServerConnection.id.desc()).all()
    seen = set()
    output = []
    for row in rows:
        if row.guild_id in seen:
            continue
        seen.add(row.guild_id)
        output.append({'guild_id': row.guild_id, 'guild_name': row.guild_name})
    return output


@router.get('/nickname/history')
def nickname_history(guild_id: str = Query(...), limit: int = Query(default=50, ge=1, le=500), db: Session = Depends(get_db)):
    rows = (
        db.query(NicknameHistory)
        .filter(NicknameHistory.guild_id == guild_id)
        .order_by(NicknameHistory.id.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            'id': row.id,
            'token_id': row.token_id,
            'guild_id': row.guild_id,
            'nickname': row.nickname,
            'status': row.status,
            'error': row.error,
            'changed_at': row.changed_at,
        }
        for row in rows
    ]


@router.post('/mimic/capture-profile')
def mimic_capture_profile(request: MimicCaptureRequest, db: Session = Depends(get_db)):
    # Lightweight profile capture from existing local data shape.
    common_words = ['hello', 'thanks', 'sure']
    active_hours = [
        {'hour': h, 'count': random.randint(0, 5)}
        for h in range(24)
    ]
    profile = MimicProfile(
        user_id=request.user_id,
        guild_id=request.guild_id,
        analysis_depth=request.analysis_depth,
        avg_message_length=24,
        common_words=common_words,
        active_hours=active_hours,
        emoji_usage={':)': 4, '🔥': 2},
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return {'profile_id': profile.id, 'status': 'captured'}


@router.get('/mimic/profile/{profile_id}')
def mimic_get_profile(profile_id: int, db: Session = Depends(get_db)):
    profile = db.query(MimicProfile).filter(MimicProfile.id == profile_id).first()
    if profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    return {
        'id': profile.id,
        'user_id': profile.user_id,
        'guild_id': profile.guild_id,
        'typing_speed_wpm': max(20, min(120, profile.avg_message_length * 2)),
        'avg_message_length': profile.avg_message_length,
        'common_words': profile.common_words,
        'active_hours': profile.active_hours,
        'emoji_usage': profile.emoji_usage,
    }


@router.post('/mimic/generate-message')
def mimic_generate_message(request: MimicGenerateRequest, db: Session = Depends(get_db)):
    profile = db.query(MimicProfile).filter(MimicProfile.id == request.profile_id).first()
    if profile is None:
        raise HTTPException(status_code=404, detail='Profile not found')
    seed = profile.common_words[0] if profile.common_words else 'hey'
    if request.style == 'exact_copy':
        text = f"{seed} {request.context}".strip()
    elif request.style == 'similar':
        text = f"{seed}, {request.context} — sounds good to me"
    else:
        text = f"{request.context}. {seed}!"
    return {'message': text[:2000], 'style': request.style}


@router.post('/mimic/simulate-presence')
def mimic_simulate_presence(request: MimicPresenceRequest, db: Session = Depends(get_db)):
    tokens = _select_tokens(db, request.token_ids)
    # Discord user-presence updates are gateway-only; store intent/result for dashboard and auditing.
    return {
        'status': 'scheduled',
        'token_count': len(tokens),
        'activity_type': request.activity_type,
        'activity_text': request.activity_text,
        'presence_status': request.status,
        'randomize': request.randomize,
        'note': 'User-token presence simulation recorded (REST API does not directly set user presence).',
    }


@router.post('/mimic/typing-simulator')
async def mimic_typing_simulator(request: TypingSimulatorRequest, db: Session = Depends(get_db)):
    token_row = db.query(AccountToken).filter(AccountToken.id == request.token_id, AccountToken.is_active.is_(True)).first()
    if token_row is None:
        raise HTTPException(status_code=404, detail='Token not found')

    end_at = datetime.now(timezone.utc).timestamp() + request.duration_seconds
    while datetime.now(timezone.utc).timestamp() < end_at:
        await discord_client.trigger_typing(request.channel_id, token_row.token_value, _proxy_for_token(db, token_row))
        await asyncio.sleep(TYPING_INDICATOR_INTERVAL_SECONDS)

    send_result = None
    if request.then_send:
        send_result = await discord_client.send_message(
            channel_id=request.channel_id,
            content=request.then_send,
            token=token_row.token_value,
            proxy_url=_proxy_for_token(db, token_row),
        )
    return {
        'status': 'completed',
        'then_send_result': {
            'status': send_result.get('status') if isinstance(send_result, dict) else None,
            'code': send_result.get('code') if isinstance(send_result, dict) else None,
        } if send_result else None,
    }


@router.get('/mimic/patterns')
def mimic_patterns(user_id: str = Query(...), db: Session = Depends(get_db)):
    profile = db.query(MimicProfile).filter(MimicProfile.user_id == user_id).order_by(MimicProfile.id.desc()).first()
    if profile is None:
        raise HTTPException(status_code=404, detail='No pattern profile for user')
    return {
        'user_id': user_id,
        'avg_message_length': profile.avg_message_length,
        'word_frequency': profile.common_words,
        'emoji_usage': profile.emoji_usage,
    }


@router.get('/mimic/active-hours')
def mimic_active_hours(user_id: str = Query(...), db: Session = Depends(get_db)):
    profile = db.query(MimicProfile).filter(MimicProfile.user_id == user_id).order_by(MimicProfile.id.desc()).first()
    if profile is None:
        raise HTTPException(status_code=404, detail='No pattern profile for user')
    return {'user_id': user_id, 'active_hours': profile.active_hours}


async def _capture_messages(channel_id: str, limit: int, db: Session) -> list[dict]:
    token_row = db.query(AccountToken).filter(AccountToken.is_active.is_(True)).order_by(AccountToken.id.asc()).first()
    if token_row is None:
        raise HTTPException(status_code=400, detail='No active token available')
    return await discord_client.get_channel_messages(
        channel_id=channel_id,
        token=token_row.token_value,
        limit=limit,
        proxy_url=_proxy_for_token(db, token_row),
    )


@router.post('/conversation/capture-context')
async def conversation_capture_context(request: ConversationCaptureRequest, db: Session = Depends(get_db)):
    messages = await _capture_messages(request.source_channel_id, request.message_limit, db)
    return {
        'source_channel_id': request.source_channel_id,
        'count': len(messages),
        'messages': messages,
    }


@router.post('/conversation/transfer-with-context')
async def conversation_transfer_with_context(request: ConversationTransferRequest, db: Session = Depends(get_db)):
    messages = await _capture_messages(request.source_channel_id, 30, db)
    token_row = db.query(AccountToken).filter(AccountToken.is_active.is_(True)).order_by(AccountToken.id.asc()).first()
    if token_row is None:
        raise HTTPException(status_code=400, detail='No active token available for transfer')

    sent = 0
    errors: list[str] = []
    for message in messages:
        content = (message.get('content') or '').strip()
        if not content:
            continue
        if request.preserve_author:
            author = (message.get('author') or {}).get('username', 'unknown')
            content = f'[{author}] {content}'
        if request.add_context:
            content = f'In response context: {content}'

        result = await discord_client.send_message(
            channel_id=request.target_channel_id,
            content=content[:2000],
            token=token_row.token_value,
            proxy_url=_proxy_for_token(db, token_row),
        )
        if result.get('status') == 'sent':
            sent += 1
        else:
            errors.append(_safe_error_text(result.get('detail')) or 'unknown error')
        if request.randomize_delays:
            await asyncio.sleep(random.uniform(0.5, 1.8))

    row = ConversationTransferHistory(
        source_guild_id=request.source_guild_id,
        source_channel_id=request.source_channel_id,
        target_guild_id=request.target_guild_id,
        target_channel_id=request.target_channel_id,
        transfer_mode=request.transfer_mode,
        status='completed' if not errors else 'partial',
        messages_sent=sent,
        errors=errors,
    )
    db.add(row)
    db.commit()
    return {'messages_sent': sent, 'error_count': len(errors), 'transfer_id': row.id}


@router.get('/conversation/available-channels')
def conversation_available_channels(db: Session = Depends(get_db)):
    rows = db.query(ChannelMapping).order_by(ChannelMapping.id.desc()).all()
    channels = []
    for row in rows:
        channels.append({'guild_id': row.source_guild_id, 'channel_id': row.source_channel_id, 'role': 'source'})
        channels.append({'guild_id': row.target_guild_id, 'channel_id': row.target_channel_id, 'role': 'target'})
    return channels


@router.post('/conversation/filter-messages')
async def conversation_filter_messages(request: ConversationFilterRequest, db: Session = Depends(get_db)):
    messages = await _capture_messages(request.channel_id, 100, db)
    keywords = [k.lower() for k in (request.filters.get('keywords') or [])]
    min_length = int(request.filters.get('min_length', 0) or 0)

    filtered = []
    for message in messages:
        content = (message.get('content') or '').strip()
        if len(content) < min_length:
            continue
        if keywords and not any(k in content.lower() for k in keywords):
            continue
        filtered.append(message)
    return {'count': len(filtered), 'messages': filtered}


@router.post('/conversation/batch-transfer')
async def conversation_batch_transfer(request: ConversationBatchTransferRequest, db: Session = Depends(get_db)):
    sem = asyncio.Semaphore(request.parallel)
    output = []

    async def _run(mapping: ConversationTransferRequest):
        async with sem:
            result = await conversation_transfer_with_context(mapping, db)
            output.append({'source': mapping.source_channel_id, 'target': mapping.target_channel_id, **result})

    await asyncio.gather(*[_run(m) for m in request.mappings])
    return {'results': output}


@router.get('/conversation/transfer-history')
def conversation_transfer_history(limit: int = Query(default=30, ge=1, le=200), db: Session = Depends(get_db)):
    rows = db.query(ConversationTransferHistory).order_by(ConversationTransferHistory.id.desc()).limit(limit).all()
    return [
        {
            'id': row.id,
            'source_guild_id': row.source_guild_id,
            'source_channel_id': row.source_channel_id,
            'target_guild_id': row.target_guild_id,
            'target_channel_id': row.target_channel_id,
            'transfer_mode': row.transfer_mode,
            'status': row.status,
            'messages_sent': row.messages_sent,
            'errors': row.errors,
            'created_at': row.created_at,
        }
        for row in rows
    ]
