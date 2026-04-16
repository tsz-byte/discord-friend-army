import asyncio
import base64
import json
import logging
import random

import httpx

from app.core.config import get_settings

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

# X-Super-Properties mimics a standard web-client fingerprint.
_SUPER_PROPERTIES = base64.b64encode(
    json.dumps(
        {
            'os': 'Windows',
            'browser': 'Chrome',
            'device': '',
            'system_locale': 'en-US',
            'browser_user_agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
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
        self.token = settings.discord_bot_token

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
    ) -> dict:
        """Join a guild via invite code with a user token.

        After a successful join the method automatically completes server
        onboarding so the account is immediately able to send messages even if
        the server uses Discord's onboarding gate.
        """
        # Strip full URL down to just the code if needed.
        code = invite_code.strip().rstrip('/')
        if '/' in code:
            code = code.rsplit('/', 1)[-1]

        headers = {
            'Authorization': token,
            'Content-Type': 'application/json',
            'X-Context-Properties': _CONTEXT_PROPERTIES,
            'X-Super-Properties': _SUPER_PROPERTIES,
            'X-Discord-Locale': 'en-US',
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/124.0.0.0 Safari/537.36'
            ),
        }
        max_attempts = 3
        async with httpx.AsyncClient(timeout=25, proxy=proxy_url) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = await client.post(
                        f'{self.base_url}/invites/{code}',
                        headers=headers,
                        json={},
                    )
                    if resp.status_code in (200, 201):
                        data = resp.json()
                        guild_info = data.get('guild') or {}
                        guild_id = guild_info.get('id') or data.get('guild_id', '')
                        if guild_id:
                            onboarding_ok = await self.complete_onboarding(guild_id, token, proxy_url)
                            logger.info('Joined guild %s (onboarding_ok=%s)', guild_id, onboarding_ok)
                        return {'status': 'joined', 'guild': guild_info}
                    if resp.status_code == 204:
                        return {'status': 'already_joined'}
                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                        await self._sleep_before_retry(attempt)
                        continue
                    return {
                        'status': 'failed',
                        'code': resp.status_code,
                        'detail': json.dumps(self._response_error_payload(resp), ensure_ascii=False),
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
            try:
                resp = await client.post(
                    f'{self.base_url}/channels/{channel_id}/messages',
                    headers=headers,
                    json={'content': content},
                )
                if resp.status_code in (200, 201):
                    return {'status': 'sent', 'message': resp.json()}
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
        token: str,
        after: str | None = None,
        limit: int = 50,
        proxy_url: str | None = None,
    ) -> list[dict]:
        """Fetch recent messages from a Discord channel using a user token.

        Returns messages in ascending order (oldest first).  Returns an empty
        list on any error so callers can degrade gracefully.
        """
        headers = {'Authorization': token}
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

    @staticmethod
    async def _sleep_before_retry(attempt: int) -> None:
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
