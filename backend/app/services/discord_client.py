import asyncio
import base64
import json
import logging
import random
import re

import httpx

from app.core.config import get_settings
from app.services.captcha_solver import CaptchaSolverService

logger = logging.getLogger('discord_research.discord_client')
RETRY_BASE_DELAY_SECONDS = 0.5
RETRY_MAX_SLEEP_SECONDS = 2.0
RETRY_JITTER_SECONDS = 0.2

# Pre-computed base64 headers required by Discord's user-token invite endpoint.
# X-Context-Properties tells Discord where the join action originates.
_CONTEXT_PROPERTIES = base64.b64encode(
    json.dumps(
        {
            'location': 'Join Guild',
            'location_guild_id': None,
            'location_channel_id': None,
            'location_channel_type': None,
        },
        separators=(',', ':'),
    ).encode()
).decode()

_USER_AGENT = (
    # Keep the Chrome version in sync with current stable Chrome releases
    # to avoid outdated fingerprints being flagged by Discord.
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

# X-Super-Properties mimics a standard web-client fingerprint.
# client_build_number corresponds to a specific Discord web-client build;
# update it periodically by reading window.GLOBAL_ENV.BUILD_NUMBER in the
# Discord web app to keep the fingerprint current.
_SUPER_PROPERTIES = base64.b64encode(
    json.dumps(
        {
            'os': 'Windows',
            'browser': 'Chrome',
            'device': '',
            'system_locale': 'en-US',
            'browser_user_agent': _USER_AGENT,
            'browser_version': '124.0.0.0',
            'os_version': '10',
            'referrer': '',
            'referring_domain': '',
            'referrer_current': '',
            'referring_domain_current': '',
            'release_channel': 'stable',
            'client_build_number': 294707,
            'client_event_source': None,
        },
        separators=(',', ':'),
    ).encode()
).decode()


class DiscordClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.discord_api_base_url.rstrip('/')
        self.token = (settings.discord_bot_token or '').strip()
        self.runtype = settings.runtype
        self.captcha_solver = CaptchaSolverService()

    async def get_guild(self, guild_id: str) -> dict:
        if not self.token:
            return {'id': guild_id, 'name': 'Unknown (token missing)'}
        headers = {'Authorization': f'Bot {self.token}'}
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                response = await client.get(f'{self.base_url}/guilds/{guild_id}', headers=headers)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError:
                return {'id': guild_id, 'name': 'Unknown (discord api unavailable)'}

    async def get_guild_onboarding(self, guild_id: str, token: str) -> dict:
        """Return the onboarding config for a guild, using a user token."""
        headers = {'Authorization': token}
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                response = await client.get(
                    f'{self.base_url}/guilds/{guild_id}/onboarding',
                    headers=headers,
                )
                if response.status_code == 200:
                    return response.json()
            except httpx.HTTPError as exc:
                logger.debug('get_guild_onboarding error guild=%s: %s', guild_id, exc)
        return {'enabled': False, 'prompts': [], 'default_channel_ids': []}

    async def complete_onboarding(
        self,
        guild_id: str,
        token: str,
        proxy_url: str | None = None,
    ) -> bool:
        """Auto-complete server onboarding for a user token.

        Selects the first available option for every prompt so the account is
        no longer gated from sending messages.  Returns True if onboarding was
        completed (or was not required), False on unexpected errors.
        """
        onboarding = await self.get_guild_onboarding(guild_id, token)
        if not onboarding.get('enabled'):
            return True  # nothing to do

        prompts = onboarding.get('prompts', [])
        if not prompts:
            return True

        onboarding_responses: dict[str, list[str]] = {}
        seen_prompts: list[str] = []
        seen_responses: list[str] = []

        for prompt in prompts:
            prompt_id = str(prompt.get('id', ''))
            options = prompt.get('options', [])
            if not prompt_id or not options:
                continue
            # Pick the first available option for each prompt; if multiple
            # selections are allowed we still pick just one to satisfy
            # "required" prompts without overfitting.
            selected_id = str(options[0]['id'])
            onboarding_responses[prompt_id] = [selected_id]
            seen_prompts.append(prompt_id)
            seen_responses.append(selected_id)

        payload = {
            'onboarding_responses': onboarding_responses,
            'onboarding_prompts_seen': seen_prompts,
            'onboarding_responses_seen': seen_responses,
        }
        headers = {'Authorization': token, 'Content-Type': 'application/json'}

        max_attempts = 3
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = await client.post(
                        f'{self.base_url}/guilds/{guild_id}/complete-onboarding',
                        headers=headers,
                        json=payload,
                    )
                    if resp.status_code in (200, 201, 204):
                        logger.info('Onboarding completed for guild %s', guild_id)
                        return True
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                        await self._sleep_before_retry(attempt)
                        continue
                    if resp.status_code == 403:
                        error_payload = self._response_error_payload(resp)
                        if error_payload.get('code') == 50001:
                            logger.info('Onboarding not available or no access for guild %s', guild_id)
                            return True
                    logger.warning(
                        'complete_onboarding guild=%s status=%s body=%s',
                        guild_id,
                        resp.status_code,
                        resp.text[:200],
                    )
                    break
                except httpx.HTTPError as exc:
                    if attempt < max_attempts:
                        await self._sleep_before_retry(attempt)
                        continue
                    logger.warning('complete_onboarding HTTP error guild=%s: %s', guild_id, exc)
        return False

    async def join_guild_via_invite(
        self,
        invite_code: str,
        token: str,
        proxy_url: str | None = None,
        token_id: int | None = None,
        guild_id: str | None = None,
        db=None,
    ) -> dict:
        """Join a guild via invite code with a user token.

        After a successful join the method automatically completes server
        onboarding so the account is immediately able to send messages even if
        the server uses Discord's onboarding gate.
        """
        code = self.extract_invite_code(invite_code)
        if not code:
            return {'status': 'failed', 'code': 400, 'detail': 'Invalid invite code format'}

        headers = {
            'Authorization': token,
            'Content-Type': 'application/json',
            'X-Context-Properties': _CONTEXT_PROPERTIES,
            'X-Super-Properties': _SUPER_PROPERTIES,
            'X-Discord-Locale': 'en-US',
            'User-Agent': _USER_AGENT,
        }
        max_attempts = 5
        captcha_payload: dict = {}
        captcha_attempts = 0
        max_captcha_attempts = 2
        async with httpx.AsyncClient(timeout=25, proxy=proxy_url) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = await client.post(
                        f'{self.base_url}/invites/{code}',
                        headers=headers,
                        json=captcha_payload or {},
                    )
                    if resp.status_code in (200, 201):
                        data = resp.json()
                        guild_info = data.get('guild') or {}
                        joined_guild_id = guild_info.get('id') or data.get('guild_id', '')
                        if not joined_guild_id:
                            return {'status': 'failed', 'code': 502, 'detail': 'Join succeeded but guild_id missing in Discord response'}
                        access_check = await self.validate_guild_access(guild_id=joined_guild_id, token=token, proxy_url=proxy_url)
                        if joined_guild_id:
                            onboarding_ok = await self.complete_onboarding(joined_guild_id, token, proxy_url)
                            logger.info('Joined guild %s (onboarding_ok=%s access=%s)', joined_guild_id, onboarding_ok, access_check.get('status'))
                        if access_check.get('status') == 'denied':
                            return {
                                'status': 'failed',
                                'code': 403,
                                'error_code': 50001,
                                'detail': access_check.get('detail', 'Missing access to server channels'),
                                'guild': guild_info,
                            }
                        return {'status': 'joined', 'guild': guild_info}
                    if resp.status_code == 204:
                        return {'status': 'already_joined'}

                    error_payload = self._response_error_payload(resp)
                    if (
                        self.captcha_solver.is_captcha_challenge(error_payload)
                        and self.captcha_solver.is_enabled
                        and captcha_attempts < max_captcha_attempts
                    ):
                        captcha_attempts += 1
                        logger.info(
                            'Discord join captcha challenge detected invite=%s token_id=%s guild_id=%s attempt=%s',
                            code,
                            token_id,
                            guild_id,
                            captcha_attempts,
                        )
                        solve_result = await self.captcha_solver.solve_discord_challenge(
                            error_payload,
                            token_id=token_id,
                            guild_id=guild_id,
                            user_agent=_USER_AGENT,
                            db=db,
                        )
                        if solve_result.get('status') == 'ready':
                            captcha_payload = {'captcha_key': solve_result.get('captcha_key')}
                            if solve_result.get('captcha_rqtoken'):
                                captcha_payload['captcha_rqtoken'] = solve_result.get('captcha_rqtoken')
                            captcha_rqdata = solve_result.get('captcha_rqdata')
                            if captcha_rqdata:
                                # Some Discord challenges require rqdata to be echoed
                                # alongside the solved token on retry.
                                captcha_payload['captcha_rqdata'] = captcha_rqdata
                            continue
                        logger.warning(
                            'Discord join captcha solve failed invite=%s token_id=%s guild_id=%s detail=%s',
                            code,
                            token_id,
                            guild_id,
                            solve_result.get('detail'),
                        )
                        return {
                            'status': 'failed',
                            'code': resp.status_code,
                            'detail': f"Captcha solve failed: {solve_result.get('detail', 'unknown error')}",
                        }

                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                        retry_after_seconds = self._retry_after_seconds(resp)
                        await self._sleep_before_retry(attempt, retry_after_seconds=retry_after_seconds)
                        continue
                    return {
                        'status': 'failed',
                        'code': resp.status_code,
                        'detail': json.dumps(error_payload, ensure_ascii=False),
                    }
                except httpx.HTTPError as exc:
                    if attempt < max_attempts:
                        await self._sleep_before_retry(attempt)
                        continue
                    return {'status': 'error', 'detail': str(exc)}

    async def send_message(
        self,
        channel_id: str,
        content: str,
        token: str,
        proxy_url: str | None = None,
    ) -> dict:
        """Send a message to a Discord channel using a user token."""
        headers = {'Authorization': token, 'Content-Type': 'application/json'}
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            max_attempts = 4
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = await client.post(
                        f'{self.base_url}/channels/{channel_id}/messages',
                        headers=headers,
                        json={'content': content},
                    )
                    if resp.status_code in (200, 201):
                        return {'status': 'sent', 'message': resp.json()}
                    if resp.status_code in (401, 403):
                        payload = self._response_error_payload(resp)
                        return {
                            'status': 'failed',
                            'code': resp.status_code,
                            'error_code': payload.get('code'),
                            'detail': payload.get('message', resp.text[:200]),
                        }
                    if resp.status_code == 429 and attempt < max_attempts:
                        await self._sleep_before_retry(attempt, retry_after_seconds=self._retry_after_seconds(resp))
                        continue
                    if resp.status_code >= 500 and attempt < max_attempts:
                        await self._sleep_before_retry(attempt)
                        continue
                    return {'status': 'failed', 'code': resp.status_code, 'detail': resp.text[:200]}
                except httpx.HTTPError as exc:
                    if attempt < max_attempts:
                        await self._sleep_before_retry(attempt)
                        continue
                    return {'status': 'error', 'detail': str(exc)}

    async def get_or_create_channel_webhook(
        self,
        channel_id: str,
        bot_token: str | None = None,
        webhook_name: str = 'DFA Mirror',
    ) -> dict:
        """Return a reusable webhook for a target channel."""
        raw = (bot_token or self.token or '').strip()
        if not raw:
            return {'status': 'failed', 'detail': 'bot token missing'}
        # Normalize: strip an existing 'Bot ' prefix before re-adding so we never
        # produce a double-prefixed value like 'Bot Bot <token>'.
        bare = raw[4:] if raw.lower().startswith('bot ') else raw
        headers = {'Authorization': f'Bot {bare}', 'Content-Type': 'application/json'}
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                list_resp = await client.get(f'{self.base_url}/channels/{channel_id}/webhooks', headers=headers)
                if list_resp.status_code == 200:
                    hooks = list_resp.json() if isinstance(list_resp.json(), list) else []
                    for hook in hooks:
                        hook_token = hook.get('token')
                        if hook.get('type') == 1 and hook_token and hook.get('name') == webhook_name:
                            return {
                                'status': 'ok',
                                'webhook_id': str(hook.get('id')),
                                'webhook_token': hook_token,
                                'url': f"{self.base_url}/webhooks/{hook.get('id')}/{hook_token}",
                            }
                create_resp = await client.post(
                    f'{self.base_url}/channels/{channel_id}/webhooks',
                    headers=headers,
                    json={'name': webhook_name},
                )
                if create_resp.status_code in (200, 201):
                    hook = create_resp.json()
                    hook_token = hook.get('token')
                    if hook_token:
                        return {
                            'status': 'ok',
                            'webhook_id': str(hook.get('id')),
                            'webhook_token': hook_token,
                            'url': f"{self.base_url}/webhooks/{hook.get('id')}/{hook_token}",
                        }
                return {'status': 'failed', 'code': create_resp.status_code, 'detail': create_resp.text[:200]}
            except httpx.HTTPError as exc:
                return {'status': 'error', 'detail': str(exc)}

    async def send_webhook_message(
        self,
        channel_id: str,
        content: str,
        username: str,
        avatar_url: str | None = None,
        timestamp_iso: str | None = None,
        bot_token: str | None = None,
    ) -> dict:
        """Send a message through a channel webhook while spoofing author identity."""
        webhook = await self.get_or_create_channel_webhook(channel_id=channel_id, bot_token=bot_token)
        if webhook.get('status') != 'ok':
            return webhook
        body_content = content
        if timestamp_iso:
            body_content = f'[{timestamp_iso}] {content}'
        payload = {
            'content': body_content[:2000],
            'username': (username or 'Unknown')[:80],
            'allowed_mentions': {'parse': []},
        }
        if avatar_url:
            payload['avatar_url'] = avatar_url
        async with httpx.AsyncClient(timeout=20) as client:
            try:
                resp = await client.post(f"{webhook['url']}?wait=true", json=payload)
                if resp.status_code in (200, 201, 204):
                    return {'status': 'sent', 'message': resp.json() if resp.content else {}}
                return {'status': 'failed', 'code': resp.status_code, 'detail': resp.text[:200]}
            except httpx.HTTPError as exc:
                return {'status': 'error', 'detail': str(exc)}

    async def get_guild_members(
        self,
        guild_id: str,
        token: str,
        proxy_url: str | None = None,
        limit: int = 100,
    ) -> list[str]:
        """Return a list of member display names (or usernames) for a guild.

        Uses the /guilds/{id}/members endpoint available to user tokens.
        Returns an empty list on any error so callers can degrade gracefully.
        """
        headers = {'Authorization': token}
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            try:
                resp = await client.get(
                    f'{self.base_url}/guilds/{guild_id}/members',
                    headers=headers,
                    params={'limit': min(limit, 1000)},
                )
                if resp.status_code == 200:
                    members = resp.json()
                    names: list[str] = []
                    for member in members:
                        nick = member.get('nick')
                        user = member.get('user') or {}
                        display = nick or user.get('global_name') or user.get('username') or ''
                        if display:
                            names.append(display)
                    return names
            except httpx.HTTPError as exc:
                logger.debug('get_guild_members error guild=%s: %s', guild_id, exc)
        return []

    async def get_channel_messages(
        self,
        channel_id: str,
        token: str | None = None,
        after: str | None = None,
        limit: int = 50,
        proxy_url: str | None = None,
    ) -> list[dict]:
        """Fetch recent messages from a Discord channel using a user token.

        Returns messages in ascending order (oldest first).  Returns an empty
        list on any error so callers can degrade gracefully.
        """
        auth_token = (token or '').strip()
        if auth_token:
            if self.runtype == 'BOTT' and not auth_token.lower().startswith('bot '):
                auth_token = f'Bot {auth_token}'
            headers = {'Authorization': auth_token}
        elif self.token:
            headers = {'Authorization': f'Bot {self.token}'}
        else:
            return []
        params: dict = {'limit': min(limit, 100)}
        if after:
            params['after'] = after
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            try:
                resp = await client.get(
                    f'{self.base_url}/channels/{channel_id}/messages',
                    headers=headers,
                    params=params,
                )
                if resp.status_code == 200:
                    messages = resp.json()
                    # Discord returns newest first; sort to oldest first for sequential processing.
                    messages.sort(key=lambda m: m.get('id', '0'))
                    return messages
                logger.debug(
                    'get_channel_messages channel=%s status=%s',
                    channel_id,
                    resp.status_code,
                )
            except httpx.HTTPError as exc:
                logger.debug('get_channel_messages error channel=%s: %s', channel_id, exc)
        return []

    async def validate_guild_access(self, guild_id: str, token: str, proxy_url: str | None = None) -> dict:
        """Validate that a token can access guild channels after joining."""
        headers = {'Authorization': token}
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            try:
                resp = await client.get(f'{self.base_url}/guilds/{guild_id}/channels', headers=headers)
                if resp.status_code == 200:
                    return {'status': 'ok'}
                if resp.status_code in (401, 403):
                    payload = self._response_error_payload(resp)
                    return {
                        'status': 'denied',
                        'code': resp.status_code,
                        'error_code': payload.get('code'),
                        'detail': payload.get('message', 'Access denied'),
                    }
                return {'status': 'unknown', 'code': resp.status_code}
            except httpx.HTTPError as exc:
                return {'status': 'error', 'detail': str(exc)}

    async def patch_user_clan_tag(self, token: str, clan_tag: str | None, proxy_url: str | None = None) -> dict:
        headers = {'Authorization': token, 'Content-Type': 'application/json'}
        payload = {'clan': clan_tag}
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            try:
                resp = await client.patch(f'{self.base_url}/users/@me', headers=headers, json=payload)
                if resp.status_code in (200, 201):
                    return {'status': 'updated', 'user': resp.json()}
                return {'status': 'failed', 'code': resp.status_code, 'detail': resp.text[:200]}
            except httpx.HTTPError as exc:
                return {'status': 'error', 'detail': str(exc)}

    async def patch_member_nickname(
        self,
        guild_id: str,
        user_id: str,
        nickname: str | None,
        token: str,
        proxy_url: str | None = None,
    ) -> dict:
        headers = {'Authorization': token, 'Content-Type': 'application/json'}
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            try:
                resp = await client.patch(
                    f'{self.base_url}/guilds/{guild_id}/members/@me',
                    headers=headers,
                    json={'nick': nickname},
                )
                if resp.status_code in (200, 204):
                    return {'status': 'updated'}
                return {'status': 'failed', 'code': resp.status_code, 'detail': resp.text[:200]}
            except httpx.HTTPError as exc:
                return {'status': 'error', 'detail': str(exc)}

    async def trigger_typing(self, channel_id: str, token: str, proxy_url: str | None = None) -> dict:
        headers = {'Authorization': token}
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            try:
                resp = await client.post(f'{self.base_url}/channels/{channel_id}/typing', headers=headers)
                if resp.status_code in (200, 204):
                    return {'status': 'ok'}
                return {'status': 'failed', 'code': resp.status_code, 'detail': resp.text[:200]}
            except httpx.HTTPError as exc:
                return {'status': 'error', 'detail': str(exc)}

    @staticmethod
    def extract_invite_code(invite: str) -> str:
        value = invite.strip()
        if not value:
            return ''
        value = value.rstrip('/')
        if '://' in value or '/' in value:
            value = value.rsplit('/', 1)[-1]
        value = value.split('?', 1)[0]
        return value if re.fullmatch(r'[a-zA-Z0-9-]{2,100}', value) else ''

    @staticmethod
    async def _sleep_before_retry(attempt: int, retry_after_seconds: float | None = None) -> None:
        if retry_after_seconds is not None and retry_after_seconds > 0:
            await asyncio.sleep(min(10.0, retry_after_seconds + random.uniform(0.0, RETRY_JITTER_SECONDS)))
            return
        await asyncio.sleep(min(RETRY_MAX_SLEEP_SECONDS, RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))) + random.uniform(0.0, RETRY_JITTER_SECONDS))

    @staticmethod
    def _response_error_payload(response: httpx.Response) -> dict:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                return payload
        except ValueError:
            pass
        return {'message': response.text[:200]}

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        retry_after = response.headers.get('Retry-After')
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        payload = DiscordClient._response_error_payload(response)
        value = payload.get('retry_after')
        if isinstance(value, (int, float)):
            return float(value)
        return None
