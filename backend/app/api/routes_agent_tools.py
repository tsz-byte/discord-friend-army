from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case
from sqlalchemy.orm import Session

from app.db.session import SessionLocal, get_db
from app.models.research import AccountToken, ScheduledMessage
from app.services.discord_client import DiscordClient
from app.services.token_manager import TokenManagerService

router = APIRouter(prefix='/agent', tags=['agent-tools'])
discord_client = DiscordClient()
token_manager = TokenManagerService()
_scheduler_task: asyncio.Task | None = None
_NUMERIC_ID_RE = re.compile(r'^\d+$')


def _parse_color(color: int | str | None) -> int | None:
    if color is None:
        return None
    if isinstance(color, int):
        return color
    hex_str = str(color).strip().lstrip('#')
    if not hex_str:
        return None
    return int(hex_str, 16)


def _normalize_timestamp(timestamp: str | bool | None) -> str | None:
    if timestamp is True:
        return datetime.now(timezone.utc).isoformat()
    if timestamp in (False, None):
        return None
    return str(timestamp)


def _proxy_for_token(token_row: AccountToken) -> str | None:
    if token_row.proxy_host and token_row.proxy_port:
        return token_manager.build_proxy_url(
            host=token_row.proxy_host,
            port=token_row.proxy_port,
            username=token_row.proxy_username or '',
            password=token_row.proxy_password or '',
        )
    return None


def _safe_agent_result(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key == 'detail' and isinstance(item, str):
                lowered = item.lower()
                if 'traceback' in lowered or 'file \"' in lowered:
                    cleaned[key] = 'internal_error'
                else:
                    cleaned[key] = item[:500]
            else:
                cleaned[key] = _safe_agent_result(item)
        return cleaned
    if isinstance(value, list):
        return [_safe_agent_result(item) for item in value]
    return value


def _select_token(db: Session, token_id: int | None = None) -> AccountToken:
    query = db.query(AccountToken).filter(AccountToken.is_active.is_(True))
    if token_id is not None:
        query = query.filter(AccountToken.id == token_id)
    else:
        rank = case(
            (AccountToken.health_status == 'healthy', 0),
            (AccountToken.health_status == 'unknown', 1),
            else_=2,
        )
        query = query.order_by(rank.asc(), AccountToken.usage_count.asc(), AccountToken.rotation_priority.asc())
    token_row = query.first()
    if token_row is None:
        if token_id is None:
            raise HTTPException(status_code=404, detail='No active token available')
        raise HTTPException(status_code=404, detail=f'token_id {token_id} not found or inactive')
    return token_row


async def _resolve_channel_id(
    channel_id_or_name: str,
    guild_id: str | None,
    db: Session,
) -> str:
    """
    If channel_id_or_name is already a numeric ID string, return it.
    Otherwise, fetch the guild channel list and find by name.
    Returns the resolved channel ID string.
    """
    value = str(channel_id_or_name).strip()
    if _NUMERIC_ID_RE.fullmatch(value):
        return value
    if not guild_id:
        raise HTTPException(status_code=400, detail='guild_id is required when channel_id is not numeric')

    token_row = _select_token(db)
    channels = await discord_client.get_guild_channels(
        guild_id=guild_id,
        token=token_row.token_value,
        proxy_url=_proxy_for_token(token_row),
    )
    if channels.get('status') != 'ok':
        raise HTTPException(status_code=400, detail=f'Unable to list guild channels: {channels.get("detail")}')

    normalized = value.lstrip('#').strip().lower()
    for channel in channels.get('channels', []):
        if str(channel.get('name', '')).lower() == normalized:
            return str(channel['id'])
    raise HTTPException(status_code=404, detail=f'Channel "{channel_id_or_name}" not found in guild {guild_id}')


class ChannelSendEmbedRequest(BaseModel):
    channel_id: str
    guild_id: str | None = None
    token_id: int | None = None
    content: str | None = None
    title: str | None = None
    description: str | None = None
    url: str | None = None
    color: int | str | None = None
    timestamp: str | bool | None = None
    author_name: str | None = None
    author_url: str | None = None
    author_icon_url: str | None = None
    footer_text: str | None = None
    footer_icon_url: str | None = None
    thumbnail_url: str | None = None
    image_url: str | None = None
    fields: list[dict] | None = None
    mention_everyone: bool = False
    mention_roles: list[str] | None = None
    mention_users: list[str] | None = None
    embeds: list[dict] | None = None


class ChannelSendMessageRequest(BaseModel):
    channel_id: str
    guild_id: str | None = None
    content: str
    token_id: int | None = None
    mention_everyone: bool = False
    mention_roles: list[str] | None = None
    mention_users: list[str] | None = None
    tts: bool = False


class ChannelSendDMRequest(BaseModel):
    user_id: str
    content: str | None = None
    token_id: int | None = None
    title: str | None = None
    description: str | None = None
    color: int | None = None
    fields: list[dict] | None = None


class ChannelDeleteRequest(BaseModel):
    channel_id: str
    guild_id: str | None = None
    reason: str | None = None
    bot_token: str | None = None


class ChannelCreateRequest(BaseModel):
    guild_id: str
    name: str
    type: int = 0
    topic: str | None = None
    category_id: str | None = None
    position: int | None = None
    nsfw: bool = False
    slowmode_delay: int = 0
    bot_token: str | None = None


class ChannelEditRequest(BaseModel):
    channel_id: str
    name: str | None = None
    topic: str | None = None
    nsfw: bool | None = None
    slowmode_delay: int | None = None
    position: int | None = None
    bot_token: str | None = None


class ChannelBulkSendRequest(ChannelSendEmbedRequest):
    channel_ids: list[str]
    delay_between_sends: float = 0.5
    randomize_delay: bool = True


class MessageDeleteRequest(BaseModel):
    channel_id: str
    message_id: str
    token_id: int | None = None
    bot_token: str | None = None


class MessageBulkDeleteRequest(BaseModel):
    channel_id: str
    message_ids: list[str]
    bot_token: str


class MessageEditRequest(BaseModel):
    channel_id: str
    message_id: str
    content: str | None = None
    token_id: int | None = None
    embeds: list[dict] | None = None


class MessagePinRequest(BaseModel):
    channel_id: str
    message_id: str
    token_id: int | None = None


class MessageReactRequest(BaseModel):
    channel_id: str
    message_id: str
    emoji: str
    token_id: int | None = None


class CampaignPostRequest(BaseModel):
    channel_id: str
    guild_id: str | None = None
    token_id: int | None = None
    content: str | None = None
    title: str | None = None
    description: str | None = None
    color: int | str | None = None
    fields: list[dict] | None = None
    footer_text: str | None = None
    image_url: str | None = None
    thumbnail_url: str | None = None
    author_name: str | None = None
    mention_everyone: bool = False
    mention_roles: list[str] | None = None
    mention_users: list[str] | None = None
    timestamp: bool | str | None = True
    campaign_type: str = 'general'


class CampaignInviteBoostRequest(BaseModel):
    channel_id: str
    guild_id: str | None = None
    token_id: int | None = None
    invite_link: str
    title: str | None = None
    description: str | None = None
    color: int | str | None = None
    image_url: str | None = None
    mention_everyone: bool = False
    mention_roles: list[str] | None = None
    mention_users: list[str] | None = None
    fields: list[dict] | None = None
    footer_text: str | None = None
    content: str | None = None


class CampaignReferralPostRequest(BaseModel):
    channel_id: str
    guild_id: str | None = None
    token_id: int | None = None
    invite_link: str
    referral_text: str | None = None
    title: str | None = None
    description: str | None = None
    color: int | str | None = None
    reward_description: str | None = None
    fields: list[dict] | None = None
    mention_everyone: bool = False
    mention_roles: list[str] | None = None
    content: str | None = None
    footer_text: str | None = None
    image_url: str | None = None


class CampaignSocialPushRequest(BaseModel):
    channel_id: str
    guild_id: str | None = None
    token_id: int | None = None
    platform: str | None = None
    social_url: str | None = None
    title: str | None = None
    description: str | None = None
    color: int | str | None = None
    content: str | None = None
    mention_everyone: bool = False
    mention_roles: list[str] | None = None
    fields: list[dict] | None = None
    image_url: str | None = None
    footer_text: str | None = None


class CampaignGrowthPostRequest(BaseModel):
    channel_id: str
    guild_id: str | None = None
    token_id: int | None = None
    title: str | None = None
    description: str | None = None
    color: int | str | None = None
    content: str | None = None
    invite_link: str | None = None
    perks: list[str] | None = None
    mention_everyone: bool = False
    mention_roles: list[str] | None = None
    fields: list[dict] | None = None
    image_url: str | None = None
    thumbnail_url: str | None = None
    footer_text: str | None = None


class CampaignMultiChannelRequest(BaseModel):
    campaign_type: str
    channel_ids: list[str]
    guild_id: str | None = None
    token_id: int | None = None
    content: str | None = None
    title: str | None = None
    description: str | None = None
    color: int | str | None = None
    invite_link: str | None = None
    mention_everyone: bool = False
    mention_roles: list[str] | None = None
    fields: list[dict] | None = None
    image_url: str | None = None
    footer_text: str | None = None
    delay_between_sends: float = 1.0
    randomize_delay: bool = True
    max_channels: int | None = None


class RoleChangeRequest(BaseModel):
    guild_id: str
    user_id: str
    role_id: str
    token_id: int | None = None
    bot_token: str | None = None
    reason: str | None = None


class RoleBulkAssignRequest(BaseModel):
    guild_id: str
    user_ids: list[str]
    role_id: str
    bot_token: str


class MemberKickRequest(BaseModel):
    guild_id: str
    user_id: str
    reason: str | None = None
    bot_token: str | None = None
    token_id: int | None = None


class MemberBanRequest(BaseModel):
    guild_id: str
    user_id: str
    reason: str | None = None
    delete_message_days: int = 0
    bot_token: str | None = None


class MemberUnbanRequest(BaseModel):
    guild_id: str
    user_id: str
    reason: str | None = None
    bot_token: str | None = None


class ThreadCreateRequest(BaseModel):
    channel_id: str
    name: str
    message: str | None = None
    auto_archive_duration: int = 1440
    token_id: int | None = None
    bot_token: str | None = None
    type: int = 11


class InviteCreateRequest(BaseModel):
    channel_id: str
    max_age: int = 86400
    max_uses: int = 0
    temporary: bool = False
    unique: bool = True
    token_id: int | None = None
    bot_token: str | None = None


class InviteDeleteRequest(BaseModel):
    invite_code: str
    bot_token: str | None = None


class WebhookCreateRequest(BaseModel):
    channel_id: str
    name: str = 'DFA Webhook'
    avatar_url: str | None = None
    bot_token: str | None = None


class WebhookSendRequest(BaseModel):
    webhook_url: str | None = None
    webhook_id: str | None = None
    webhook_token: str | None = None
    content: str | None = None
    username: str | None = None
    avatar_url: str | None = None
    tts: bool = False
    embeds: list[dict] | None = None
    title: str | None = None
    description: str | None = None
    color: int | str | None = None
    fields: list[dict] | None = None
    footer_text: str | None = None
    image_url: str | None = None
    thumbnail_url: str | None = None
    author_name: str | None = None
    mention_everyone: bool = False
    mention_roles: list[str] | None = None
    mention_users: list[str] | None = None


class WebhookDeleteRequest(BaseModel):
    webhook_id: str
    bot_token: str | None = None


class MessageScheduleRequest(BaseModel):
    channel_id: str
    send_at: str
    content: str | None = None
    title: str | None = None
    description: str | None = None
    color: int | str | None = None
    fields: list[dict] | None = None
    mention_everyone: bool = False
    mention_roles: list[str] | None = None
    token_id: int | None = None


async def _send_embed_with_token(
    *,
    db: Session,
    channel_id: str,
    guild_id: str | None,
    token_id: int | None,
    payload: dict[str, Any],
) -> dict:
    token_row = _select_token(db, token_id)
    resolved = await _resolve_channel_id(channel_id, guild_id, db)
    color = payload.get('color')
    if color is not None:
        payload['color'] = _parse_color(color)
    timestamp = payload.get('timestamp')
    if timestamp is not None:
        payload['timestamp'] = _normalize_timestamp(timestamp)
    return _safe_agent_result(await discord_client.send_embed(
        channel_id=resolved,
        token=token_row.token_value,
        proxy_url=_proxy_for_token(token_row),
        **payload,
    ))


@router.post('/channel/send-embed')
async def channel_send_embed(request: ChannelSendEmbedRequest, db: Session = Depends(get_db)):
    payload = request.model_dump(exclude={'channel_id', 'guild_id', 'token_id'})
    return _safe_agent_result(await _send_embed_with_token(
        db=db,
        channel_id=request.channel_id,
        guild_id=request.guild_id,
        token_id=request.token_id,
        payload=payload,
    ))


@router.post('/channel/send-message')
async def channel_send_message(request: ChannelSendMessageRequest, db: Session = Depends(get_db)):
    token_row = _select_token(db, request.token_id)
    resolved = await _resolve_channel_id(request.channel_id, request.guild_id, db)
    return _safe_agent_result(await discord_client.send_message(
        channel_id=resolved,
        content=request.content,
        token=token_row.token_value,
        proxy_url=_proxy_for_token(token_row),
        mention_everyone=request.mention_everyone,
        mention_roles=request.mention_roles,
        mention_users=request.mention_users,
        tts=request.tts,
    ))


@router.post('/channel/send-dm')
async def channel_send_dm(request: ChannelSendDMRequest, db: Session = Depends(get_db)):
    token_row = _select_token(db, request.token_id)
    return _safe_agent_result(await discord_client.send_dm(
        user_id=request.user_id,
        token=token_row.token_value,
        proxy_url=_proxy_for_token(token_row),
        content=request.content,
        title=request.title,
        description=request.description,
        color=request.color,
        fields=request.fields,
    ))


@router.delete('/channel/delete')
@router.post('/channel/delete')
async def channel_delete(request: ChannelDeleteRequest, db: Session = Depends(get_db)):
    bot_token = request.bot_token or _select_token(db).token_value
    return _safe_agent_result(await discord_client.delete_channel(channel_id=request.channel_id, bot_token=bot_token))


@router.post('/channel/create')
async def channel_create(request: ChannelCreateRequest, db: Session = Depends(get_db)):
    bot_token = request.bot_token or _select_token(db).token_value
    payload = {
        'name': request.name,
        'type': request.type,
        'topic': request.topic,
        'parent_id': request.category_id,
        'position': request.position,
        'nsfw': request.nsfw,
        'rate_limit_per_user': request.slowmode_delay,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return _safe_agent_result(await discord_client.create_channel(guild_id=request.guild_id, bot_token=bot_token, payload=payload))


@router.patch('/channel/edit')
async def channel_edit(request: ChannelEditRequest, db: Session = Depends(get_db)):
    bot_token = request.bot_token or _select_token(db).token_value
    payload = {
        'name': request.name,
        'topic': request.topic,
        'nsfw': request.nsfw,
        'rate_limit_per_user': request.slowmode_delay,
        'position': request.position,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return _safe_agent_result(await discord_client.edit_channel(channel_id=request.channel_id, bot_token=bot_token, payload=payload))


@router.get('/channel/list')
async def channel_list(guild_id: str = Query(...), token_id: int | None = Query(default=None), db: Session = Depends(get_db)):
    token_row = _select_token(db, token_id)
    return _safe_agent_result(await discord_client.get_guild_channels(
        guild_id=guild_id,
        token=token_row.token_value,
        proxy_url=_proxy_for_token(token_row),
    ))


@router.post('/channel/bulk-send')
async def channel_bulk_send(request: ChannelBulkSendRequest, db: Session = Depends(get_db)):
    channel_ids = request.channel_ids
    results = []
    payload = request.model_dump(exclude={'channel_ids', 'delay_between_sends', 'randomize_delay', 'channel_id', 'guild_id', 'token_id'})
    for idx, channel in enumerate(channel_ids):
        result = await _send_embed_with_token(
            db=db,
            channel_id=channel,
            guild_id=request.guild_id,
            token_id=request.token_id,
            payload=dict(payload),
        )
        results.append({'channel_id': channel, 'result': result})
        if idx < len(channel_ids) - 1:
            delay = request.delay_between_sends
            if request.randomize_delay:
                delay += random.uniform(0.0, min(2.0, request.delay_between_sends))
            await asyncio.sleep(max(0.0, delay))
    return {'status': 'completed', 'total': len(results), 'results': results}


@router.post('/message/delete')
async def message_delete(request: MessageDeleteRequest, db: Session = Depends(get_db)):
    if request.bot_token:
        return _safe_agent_result(await discord_client.delete_message(request.channel_id, request.message_id, request.bot_token, bot=True))
    token_row = _select_token(db, request.token_id)
    return _safe_agent_result(await discord_client.delete_message(
        request.channel_id,
        request.message_id,
        token_row.token_value,
        proxy_url=_proxy_for_token(token_row),
    ))


@router.post('/message/bulk-delete')
async def message_bulk_delete(request: MessageBulkDeleteRequest):
    return _safe_agent_result(await discord_client.bulk_delete_messages(
        channel_id=request.channel_id,
        message_ids=request.message_ids,
        bot_token=request.bot_token,
    ))


@router.patch('/message/edit')
async def message_edit(request: MessageEditRequest, db: Session = Depends(get_db)):
    token_row = _select_token(db, request.token_id)
    return _safe_agent_result(await discord_client.edit_message(
        channel_id=request.channel_id,
        message_id=request.message_id,
        token=token_row.token_value,
        content=request.content,
        embeds=request.embeds,
        proxy_url=_proxy_for_token(token_row),
    ))


@router.post('/message/pin')
async def message_pin(request: MessagePinRequest, db: Session = Depends(get_db)):
    token_row = _select_token(db, request.token_id)
    return _safe_agent_result(await discord_client.pin_message(
        channel_id=request.channel_id,
        message_id=request.message_id,
        token=token_row.token_value,
        proxy_url=_proxy_for_token(token_row),
    ))


@router.post('/message/react')
async def message_react(request: MessageReactRequest, db: Session = Depends(get_db)):
    token_row = _select_token(db, request.token_id)
    return _safe_agent_result(await discord_client.add_reaction(
        channel_id=request.channel_id,
        message_id=request.message_id,
        emoji=request.emoji,
        token=token_row.token_value,
        proxy_url=_proxy_for_token(token_row),
    ))


@router.post('/campaign/post')
async def campaign_post(request: CampaignPostRequest, db: Session = Depends(get_db)):
    payload = {
        'content': request.content,
        'title': request.title,
        'description': request.description,
        'color': request.color,
        'fields': request.fields,
        'footer_text': request.footer_text,
        'image_url': request.image_url,
        'thumbnail_url': request.thumbnail_url,
        'author_name': request.author_name,
        'mention_everyone': request.mention_everyone,
        'mention_roles': request.mention_roles,
        'mention_users': request.mention_users,
        'timestamp': _normalize_timestamp(request.timestamp),
    }
    return _safe_agent_result(await _send_embed_with_token(db=db, channel_id=request.channel_id, guild_id=request.guild_id, token_id=request.token_id, payload=payload))


@router.post('/campaign/invite-boost')
async def campaign_invite_boost(request: CampaignInviteBoostRequest, db: Session = Depends(get_db)):
    fields = list(request.fields or []) + [{'name': 'Invite Link', 'value': request.invite_link, 'inline': False}]
    payload = {
        'content': request.content,
        'title': request.title,
        'description': request.description,
        'color': request.color,
        'image_url': request.image_url,
        'mention_everyone': request.mention_everyone,
        'mention_roles': request.mention_roles,
        'mention_users': request.mention_users,
        'fields': fields,
        'footer_text': request.footer_text,
    }
    return _safe_agent_result(await _send_embed_with_token(db=db, channel_id=request.channel_id, guild_id=request.guild_id, token_id=request.token_id, payload=payload))


@router.post('/campaign/referral-post')
async def campaign_referral_post(request: CampaignReferralPostRequest, db: Session = Depends(get_db)):
    fields = list(request.fields or [])
    fields.append({'name': 'Invite Link', 'value': request.invite_link, 'inline': False})
    if request.reward_description:
        fields.append({'name': 'Reward', 'value': request.reward_description, 'inline': False})
    if request.referral_text:
        fields.append({'name': 'Referral', 'value': request.referral_text, 'inline': False})
    payload = {
        'content': request.content,
        'title': request.title,
        'description': request.description,
        'color': request.color,
        'fields': fields,
        'mention_everyone': request.mention_everyone,
        'mention_roles': request.mention_roles,
        'footer_text': request.footer_text,
        'image_url': request.image_url,
    }
    return _safe_agent_result(await _send_embed_with_token(db=db, channel_id=request.channel_id, guild_id=request.guild_id, token_id=request.token_id, payload=payload))


@router.post('/campaign/social-push')
async def campaign_social_push(request: CampaignSocialPushRequest, db: Session = Depends(get_db)):
    fields = list(request.fields or [])
    if request.platform:
        fields.append({'name': 'Platform', 'value': request.platform, 'inline': True})
    if request.social_url:
        fields.append({'name': 'Link', 'value': request.social_url, 'inline': False})
    payload = {
        'content': request.content,
        'title': request.title,
        'description': request.description,
        'color': request.color,
        'mention_everyone': request.mention_everyone,
        'mention_roles': request.mention_roles,
        'fields': fields,
        'image_url': request.image_url,
        'footer_text': request.footer_text,
    }
    return _safe_agent_result(await _send_embed_with_token(db=db, channel_id=request.channel_id, guild_id=request.guild_id, token_id=request.token_id, payload=payload))


@router.post('/campaign/growth-post')
async def campaign_growth_post(request: CampaignGrowthPostRequest, db: Session = Depends(get_db)):
    fields = list(request.fields or [])
    if request.invite_link:
        fields.append({'name': 'Invite Link', 'value': request.invite_link, 'inline': False})
    if request.perks:
        fields.append({'name': 'Perks', 'value': '\n'.join(request.perks), 'inline': False})
    payload = {
        'content': request.content,
        'title': request.title,
        'description': request.description,
        'color': request.color,
        'mention_everyone': request.mention_everyone,
        'mention_roles': request.mention_roles,
        'fields': fields,
        'image_url': request.image_url,
        'thumbnail_url': request.thumbnail_url,
        'footer_text': request.footer_text,
    }
    return _safe_agent_result(await _send_embed_with_token(db=db, channel_id=request.channel_id, guild_id=request.guild_id, token_id=request.token_id, payload=payload))


@router.post('/campaign/multi-channel')
async def campaign_multi_channel(request: CampaignMultiChannelRequest, db: Session = Depends(get_db)):
    channels = request.channel_ids
    if request.max_channels is not None:
        channels = channels[: max(0, request.max_channels)]
    results = []
    for idx, channel_id in enumerate(channels):
        if request.campaign_type == 'invite_boost':
            fields = list(request.fields or [])
            if request.invite_link:
                fields.append({'name': 'Invite Link', 'value': request.invite_link, 'inline': False})
            payload = {
                'content': request.content,
                'title': request.title,
                'description': request.description,
                'color': request.color,
                'mention_everyone': request.mention_everyone,
                'mention_roles': request.mention_roles,
                'fields': fields,
                'image_url': request.image_url,
                'footer_text': request.footer_text,
            }
        else:
            payload = {
                'content': request.content,
                'title': request.title,
                'description': request.description,
                'color': request.color,
                'mention_everyone': request.mention_everyone,
                'mention_roles': request.mention_roles,
                'fields': request.fields,
                'image_url': request.image_url,
                'footer_text': request.footer_text,
            }
        result = await _send_embed_with_token(db=db, channel_id=channel_id, guild_id=request.guild_id, token_id=request.token_id, payload=payload)
        results.append({'channel_id': channel_id, 'result': result})
        if idx < len(channels) - 1:
            delay = request.delay_between_sends
            if request.randomize_delay:
                delay += random.uniform(0.0, min(2.0, request.delay_between_sends))
            await asyncio.sleep(max(0.0, delay))
    return {'status': 'completed', 'campaign_type': request.campaign_type, 'results': results}


@router.post('/role/add')
async def role_add(request: RoleChangeRequest, db: Session = Depends(get_db)):
    bot_token = request.bot_token
    if not bot_token:
        token_row = _select_token(db, request.token_id)
        bot_token = token_row.token_value
    return _safe_agent_result(await discord_client.add_role_to_member(
        guild_id=request.guild_id,
        user_id=request.user_id,
        role_id=request.role_id,
        bot_token=bot_token,
        reason=request.reason,
    ))


@router.post('/role/remove')
async def role_remove(request: RoleChangeRequest, db: Session = Depends(get_db)):
    bot_token = request.bot_token
    if not bot_token:
        token_row = _select_token(db, request.token_id)
        bot_token = token_row.token_value
    return _safe_agent_result(await discord_client.remove_role_from_member(
        guild_id=request.guild_id,
        user_id=request.user_id,
        role_id=request.role_id,
        bot_token=bot_token,
        reason=request.reason,
    ))


@router.get('/role/list')
async def role_list(guild_id: str = Query(...), token_id: int | None = Query(default=None), db: Session = Depends(get_db)):
    token_row = _select_token(db, token_id)
    return _safe_agent_result(await discord_client.get_guild_roles(
        guild_id=guild_id,
        token=token_row.token_value,
        proxy_url=_proxy_for_token(token_row),
    ))


@router.post('/role/bulk-assign')
async def role_bulk_assign(request: RoleBulkAssignRequest):
    results = []
    for user_id in request.user_ids:
        result = await discord_client.add_role_to_member(
            guild_id=request.guild_id,
            user_id=user_id,
            role_id=request.role_id,
            bot_token=request.bot_token,
        )
        results.append({'user_id': user_id, 'result': result})
    return {'status': 'completed', 'results': results}


@router.post('/member/kick')
async def member_kick(request: MemberKickRequest, db: Session = Depends(get_db)):
    bot_token = request.bot_token
    if not bot_token:
        token_row = _select_token(db, request.token_id)
        bot_token = token_row.token_value
    return _safe_agent_result(await discord_client.kick_member(
        guild_id=request.guild_id,
        user_id=request.user_id,
        bot_token=bot_token,
        reason=request.reason,
    ))


@router.post('/member/ban')
async def member_ban(request: MemberBanRequest, db: Session = Depends(get_db)):
    bot_token = request.bot_token or _select_token(db).token_value
    return _safe_agent_result(await discord_client.ban_member(
        guild_id=request.guild_id,
        user_id=request.user_id,
        bot_token=bot_token,
        reason=request.reason,
        delete_message_days=request.delete_message_days,
    ))


@router.post('/member/unban')
async def member_unban(request: MemberUnbanRequest, db: Session = Depends(get_db)):
    bot_token = request.bot_token or _select_token(db).token_value
    return _safe_agent_result(await discord_client.unban_member(
        guild_id=request.guild_id,
        user_id=request.user_id,
        bot_token=bot_token,
        reason=request.reason,
    ))


@router.get('/member/list')
async def member_list(
    guild_id: str = Query(...),
    limit: int = Query(default=100, ge=1, le=1000),
    token_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    token_row = _select_token(db, token_id)
    return _safe_agent_result(await discord_client.get_guild_members_list(
        guild_id=guild_id,
        token=token_row.token_value,
        limit=limit,
        proxy_url=_proxy_for_token(token_row),
    ))


@router.get('/member/search')
async def member_search(
    guild_id: str = Query(...),
    query: str = Query(...),
    limit: int = Query(default=10, ge=1, le=1000),
    token_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    token_row = _select_token(db, token_id)
    return _safe_agent_result(await discord_client.search_guild_members(
        guild_id=guild_id,
        token=token_row.token_value,
        query=query,
        limit=limit,
        proxy_url=_proxy_for_token(token_row),
    ))


@router.post('/thread/create')
async def thread_create(request: ThreadCreateRequest, db: Session = Depends(get_db)):
    if request.bot_token:
        return _safe_agent_result(await discord_client.create_thread(
            channel_id=request.channel_id,
            name=request.name,
            token=request.bot_token,
            auto_archive_duration=request.auto_archive_duration,
            thread_type=request.type,
            message=request.message,
            bot=True,
        ))
    token_row = _select_token(db, request.token_id)
    return _safe_agent_result(await discord_client.create_thread(
        channel_id=request.channel_id,
        name=request.name,
        token=token_row.token_value,
        auto_archive_duration=request.auto_archive_duration,
        thread_type=request.type,
        message=request.message,
        bot=False,
    ))


@router.post('/invite/create')
async def invite_create(request: InviteCreateRequest, db: Session = Depends(get_db)):
    if request.bot_token:
        return _safe_agent_result(await discord_client.create_invite(
            channel_id=request.channel_id,
            token=request.bot_token,
            max_age=request.max_age,
            max_uses=request.max_uses,
            temporary=request.temporary,
            unique=request.unique,
            bot=True,
        ))
    token_row = _select_token(db, request.token_id)
    return _safe_agent_result(await discord_client.create_invite(
        channel_id=request.channel_id,
        token=token_row.token_value,
        max_age=request.max_age,
        max_uses=request.max_uses,
        temporary=request.temporary,
        unique=request.unique,
        bot=False,
    ))


@router.get('/invite/list')
async def invite_list(
    channel_id: str | None = Query(default=None),
    guild_id: str | None = Query(default=None),
    token_id: int | None = Query(default=None),
    db: Session = Depends(get_db),
):
    token_row = _select_token(db, token_id)
    if channel_id:
        return _safe_agent_result(await discord_client.get_channel_invites(channel_id=channel_id, token=token_row.token_value, proxy_url=_proxy_for_token(token_row)))
    if guild_id:
        return _safe_agent_result(await discord_client.get_guild_invites(guild_id=guild_id, token=token_row.token_value, proxy_url=_proxy_for_token(token_row)))
    raise HTTPException(status_code=400, detail='Either channel_id or guild_id is required')


@router.delete('/invite/delete')
@router.post('/invite/delete')
async def invite_delete(request: InviteDeleteRequest, db: Session = Depends(get_db)):
    bot_token = request.bot_token or _select_token(db).token_value
    return _safe_agent_result(await discord_client.delete_invite(invite_code=request.invite_code, bot_token=bot_token))


@router.post('/webhook/create')
async def webhook_create(request: WebhookCreateRequest, db: Session = Depends(get_db)):
    bot_token = request.bot_token or _select_token(db).token_value
    return _safe_agent_result(await discord_client.create_webhook(
        channel_id=request.channel_id,
        bot_token=bot_token,
        name=request.name,
        avatar=request.avatar_url,
    ))


@router.post('/webhook/send')
async def webhook_send(request: WebhookSendRequest):
    return _safe_agent_result(await discord_client.send_via_webhook(
        webhook_url=request.webhook_url,
        webhook_id=request.webhook_id,
        webhook_token=request.webhook_token,
        content=request.content,
        username=request.username,
        avatar_url=request.avatar_url,
        tts=request.tts,
        embeds=request.embeds,
        title=request.title,
        description=request.description,
        color=_parse_color(request.color),
        fields=request.fields,
        footer_text=request.footer_text,
        image_url=request.image_url,
        thumbnail_url=request.thumbnail_url,
        author_name=request.author_name,
        mention_everyone=request.mention_everyone,
        mention_roles=request.mention_roles,
        mention_users=request.mention_users,
    ))


@router.delete('/webhook/delete')
async def webhook_delete(request: WebhookDeleteRequest, db: Session = Depends(get_db)):
    bot_token = request.bot_token or _select_token(db).token_value
    return _safe_agent_result(await discord_client.delete_webhook(webhook_id=request.webhook_id, bot_token=bot_token))


@router.get('/webhook/list')
async def webhook_list(channel_id: str = Query(...), bot_token: str | None = Query(default=None), db: Session = Depends(get_db)):
    if not bot_token:
        bot_token = _select_token(db).token_value
    return _safe_agent_result(await discord_client.list_webhooks(channel_id=channel_id, bot_token=bot_token))


@router.get('/server/info')
async def server_info(guild_id: str = Query(...), token_id: int | None = Query(default=None), db: Session = Depends(get_db)):
    token_row = _select_token(db, token_id)
    info = await discord_client.get_guild_info(guild_id=guild_id, token=token_row.token_value, proxy_url=_proxy_for_token(token_row))
    if info.get('status') != 'ok':
        return info
    guild = info.get('guild') or {}
    return {
        'id': guild.get('id'),
        'name': guild.get('name'),
        'icon_url': info.get('icon_url'),
        'description': guild.get('description'),
        'member_count': guild.get('approximate_member_count') or guild.get('member_count'),
        'online_count': guild.get('approximate_presence_count'),
        'boost_level': guild.get('premium_tier'),
        'channels': info.get('channels', []),
        'roles': info.get('roles', []),
        'features': guild.get('features', []),
    }


@router.get('/server/channels')
async def server_channels(guild_id: str = Query(...), token_id: int | None = Query(default=None), db: Session = Depends(get_db)):
    return _safe_agent_result(await channel_list(guild_id=guild_id, token_id=token_id, db=db))


@router.get('/server/roles')
async def server_roles(guild_id: str = Query(...), token_id: int | None = Query(default=None), db: Session = Depends(get_db)):
    return _safe_agent_result(await role_list(guild_id=guild_id, token_id=token_id, db=db))


async def _deliver_scheduled_message(record: ScheduledMessage) -> tuple[str, str | None]:
    db = SessionLocal()
    try:
        db_record = db.query(ScheduledMessage).filter(ScheduledMessage.id == record.id).first()
        if db_record is None:
            return 'failed', 'scheduled message not found'
        token_row = _select_token(db, db_record.token_id)
        payload = dict(db_record.payload or {})
        color = payload.get('color')
        if color is not None:
            payload['color'] = _parse_color(color)
        result = await discord_client.send_embed(
            channel_id=str(db_record.channel_id),
            token=token_row.token_value,
            proxy_url=_proxy_for_token(token_row),
            content=payload.get('content'),
            title=payload.get('title'),
            description=payload.get('description'),
            color=payload.get('color'),
            fields=payload.get('fields'),
            mention_everyone=bool(payload.get('mention_everyone', False)),
            mention_roles=payload.get('mention_roles'),
        )
        if result.get('status') == 'sent':
            db_record.status = 'sent'
            db_record.sent_at = datetime.now(timezone.utc)
            db_record.error = None
        else:
            db_record.status = 'failed'
            db_record.error = str(result.get('detail') or result.get('code') or 'send_failed')
        db.commit()
        return db_record.status, db_record.error
    finally:
        db.close()


async def _scheduled_dispatch_loop() -> None:
    while True:
        db = SessionLocal()
        try:
            now = datetime.now(timezone.utc)
            pending = (
                db.query(ScheduledMessage)
                .filter(ScheduledMessage.status == 'pending', ScheduledMessage.send_at <= now)
                .order_by(ScheduledMessage.send_at.asc())
                .limit(50)
                .all()
            )
            for row in pending:
                await _deliver_scheduled_message(row)
        except Exception:
            pass
        finally:
            db.close()
        await asyncio.sleep(1.0)


@router.on_event('startup')
async def _start_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduled_dispatch_loop())


@router.post('/message/schedule')
async def message_schedule(request: MessageScheduleRequest, db: Session = Depends(get_db)):
    try:
        send_at = datetime.fromisoformat(request.send_at.replace('Z', '+00:00'))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f'Invalid send_at ISO datetime: {request.send_at}') from exc
    payload = {
        'content': request.content,
        'title': request.title,
        'description': request.description,
        'color': request.color,
        'fields': request.fields,
        'mention_everyone': request.mention_everyone,
        'mention_roles': request.mention_roles,
    }
    row = ScheduledMessage(
        channel_id=request.channel_id,
        token_id=request.token_id,
        send_at=send_at,
        payload=payload,
        status='pending',
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {'status': 'scheduled', 'id': row.id, 'send_at': row.send_at.isoformat()}


TOOL_LIST = [
    {'name': 'channel__send_embed', 'method': 'POST', 'endpoint': '/api/v1/agent/channel/send-embed'},
    {'name': 'channel__send_message', 'method': 'POST', 'endpoint': '/api/v1/agent/channel/send-message'},
    {'name': 'channel__send_dm', 'method': 'POST', 'endpoint': '/api/v1/agent/channel/send-dm'},
    {'name': 'channel__delete', 'method': 'POST', 'endpoint': '/api/v1/agent/channel/delete'},
    {'name': 'channel__create', 'method': 'POST', 'endpoint': '/api/v1/agent/channel/create'},
    {'name': 'channel__edit', 'method': 'PATCH', 'endpoint': '/api/v1/agent/channel/edit'},
    {'name': 'channel__list', 'method': 'GET', 'endpoint': '/api/v1/agent/channel/list'},
    {'name': 'channel__bulk_send', 'method': 'POST', 'endpoint': '/api/v1/agent/channel/bulk-send'},
    {'name': 'message__delete', 'method': 'POST', 'endpoint': '/api/v1/agent/message/delete'},
    {'name': 'message__bulk_delete', 'method': 'POST', 'endpoint': '/api/v1/agent/message/bulk-delete'},
    {'name': 'message__edit', 'method': 'PATCH', 'endpoint': '/api/v1/agent/message/edit'},
    {'name': 'message__pin', 'method': 'POST', 'endpoint': '/api/v1/agent/message/pin'},
    {'name': 'message__react', 'method': 'POST', 'endpoint': '/api/v1/agent/message/react'},
    {'name': 'message__schedule', 'method': 'POST', 'endpoint': '/api/v1/agent/message/schedule'},
    {'name': 'campaign__post', 'method': 'POST', 'endpoint': '/api/v1/agent/campaign/post'},
    {'name': 'campaign__invite_boost', 'method': 'POST', 'endpoint': '/api/v1/agent/campaign/invite-boost'},
    {'name': 'campaign__referral_post', 'method': 'POST', 'endpoint': '/api/v1/agent/campaign/referral-post'},
    {'name': 'campaign__social_push', 'method': 'POST', 'endpoint': '/api/v1/agent/campaign/social-push'},
    {'name': 'campaign__growth_post', 'method': 'POST', 'endpoint': '/api/v1/agent/campaign/growth-post'},
    {'name': 'campaign__multi_channel', 'method': 'POST', 'endpoint': '/api/v1/agent/campaign/multi-channel'},
    {'name': 'role__add', 'method': 'POST', 'endpoint': '/api/v1/agent/role/add'},
    {'name': 'role__remove', 'method': 'POST', 'endpoint': '/api/v1/agent/role/remove'},
    {'name': 'role__list', 'method': 'GET', 'endpoint': '/api/v1/agent/role/list'},
    {'name': 'role__bulk_assign', 'method': 'POST', 'endpoint': '/api/v1/agent/role/bulk-assign'},
    {'name': 'member__kick', 'method': 'POST', 'endpoint': '/api/v1/agent/member/kick'},
    {'name': 'member__ban', 'method': 'POST', 'endpoint': '/api/v1/agent/member/ban'},
    {'name': 'member__unban', 'method': 'POST', 'endpoint': '/api/v1/agent/member/unban'},
    {'name': 'member__list', 'method': 'GET', 'endpoint': '/api/v1/agent/member/list'},
    {'name': 'member__search', 'method': 'GET', 'endpoint': '/api/v1/agent/member/search'},
    {'name': 'thread__create', 'method': 'POST', 'endpoint': '/api/v1/agent/thread/create'},
    {'name': 'invite__create', 'method': 'POST', 'endpoint': '/api/v1/agent/invite/create'},
    {'name': 'invite__list', 'method': 'GET', 'endpoint': '/api/v1/agent/invite/list'},
    {'name': 'invite__delete', 'method': 'POST', 'endpoint': '/api/v1/agent/invite/delete'},
    {'name': 'webhook__create', 'method': 'POST', 'endpoint': '/api/v1/agent/webhook/create'},
    {'name': 'webhook__send', 'method': 'POST', 'endpoint': '/api/v1/agent/webhook/send'},
    {'name': 'webhook__delete', 'method': 'DELETE', 'endpoint': '/api/v1/agent/webhook/delete'},
    {'name': 'webhook__list', 'method': 'GET', 'endpoint': '/api/v1/agent/webhook/list'},
    {'name': 'server__info', 'method': 'GET', 'endpoint': '/api/v1/agent/server/info'},
    {'name': 'server__channels', 'method': 'GET', 'endpoint': '/api/v1/agent/server/channels'},
    {'name': 'server__roles', 'method': 'GET', 'endpoint': '/api/v1/agent/server/roles'},
]

TOOL_BODY_MODELS: dict[str, type[BaseModel]] = {
    'channel__send_embed': ChannelSendEmbedRequest,
    'channel__send_message': ChannelSendMessageRequest,
    'channel__send_dm': ChannelSendDMRequest,
    'channel__delete': ChannelDeleteRequest,
    'channel__create': ChannelCreateRequest,
    'channel__edit': ChannelEditRequest,
    'channel__bulk_send': ChannelBulkSendRequest,
    'message__delete': MessageDeleteRequest,
    'message__bulk_delete': MessageBulkDeleteRequest,
    'message__edit': MessageEditRequest,
    'message__pin': MessagePinRequest,
    'message__react': MessageReactRequest,
    'message__schedule': MessageScheduleRequest,
    'campaign__post': CampaignPostRequest,
    'campaign__invite_boost': CampaignInviteBoostRequest,
    'campaign__referral_post': CampaignReferralPostRequest,
    'campaign__social_push': CampaignSocialPushRequest,
    'campaign__growth_post': CampaignGrowthPostRequest,
    'campaign__multi_channel': CampaignMultiChannelRequest,
    'role__add': RoleChangeRequest,
    'role__remove': RoleChangeRequest,
    'role__bulk_assign': RoleBulkAssignRequest,
    'member__kick': MemberKickRequest,
    'member__ban': MemberBanRequest,
    'member__unban': MemberUnbanRequest,
    'thread__create': ThreadCreateRequest,
    'invite__create': InviteCreateRequest,
    'invite__delete': InviteDeleteRequest,
    'webhook__create': WebhookCreateRequest,
    'webhook__send': WebhookSendRequest,
    'webhook__delete': WebhookDeleteRequest,
}

TOOL_EXAMPLES: dict[str, dict] = {
    'channel__send_embed': {'channel_id': '123', 'title': 'Launch', 'description': 'Now live', 'color': '#FF5733'},
    'channel__send_message': {'channel_id': '123', 'content': 'Hello team'},
    'campaign__invite_boost': {'channel_id': '123', 'invite_link': 'https://discord.gg/example', 'title': 'Join us'},
    'member__kick': {'guild_id': '111', 'user_id': '222', 'reason': 'spam'},
    'webhook__send': {'webhook_url': 'https://discord.com/api/webhooks/ID/TOKEN', 'content': 'Webhook ping'},
}


@router.get('/tools/list')
def tools_list():
    return {'tools': TOOL_LIST}


@router.get('/tools/help')
def tools_help():
    detailed = []
    for tool in TOOL_LIST:
        model = TOOL_BODY_MODELS.get(tool['name'])
        param_schema = model.model_json_schema() if model else {'type': 'object', 'properties': {}}
        detailed.append(
            {
                **tool,
                'description': f"Agent tool {tool['name']} calling {tool['method']} {tool['endpoint']}",
                'parameters': param_schema,
                'example_request': TOOL_EXAMPLES.get(tool['name'], {}),
                'example_response': {'status': 'ok', 'data': {}},
            }
        )
    return {'tools': detailed}
