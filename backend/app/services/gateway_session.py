"""Discord WebSocket gateway session for obtaining a real gateway session_id.

Connects to the Discord gateway, authenticates via IDENTIFY (opcode 2),
waits for the READY event, and provides the session_id needed for join requests.
A heartbeat (opcode 1) is maintained to keep the connection alive.

Usage::

    async with GatewaySession(token, proxy=proxy_url) as gw:
        if await gw.wait_for_ready(timeout=20):
            session_id = gw.session_id   # real gateway session_id
        else:
            session_id = None            # gateway timed out → caller should fall back
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import asdict

from browserforge.fingerprints import FingerprintGenerator
from curl_cffi import AsyncSession

logger = logging.getLogger('discord_research.gateway_session')

GATEWAY_URL = 'wss://gateway.discord.gg/?v=9&encoding=json'

# Cached globally — FingerprintGenerator is expensive to initialise.
_FINGERPRINT_GENERATOR = FingerprintGenerator()
_FIREFOX_RE = re.compile(r'Firefox/([\d.]+)')


class GatewaySession:
    """Discord WebSocket gateway session (one per join attempt).

    Connects to the Discord gateway using ``curl_cffi`` with browser
    impersonation, authenticates with IDENTIFY, and sets :attr:`session_id`
    once the READY event arrives.

    Parameters
    ----------
    token:
        Discord user token.
    proxy:
        Optional proxy URL in the format ``http://user:pass@host:port``.
    user_agent:
        Optional pre-built User-Agent string from a ``_TokenFP`` profile.
        When provided, ``browser_version``, ``client_identity``, and ``locale``
        should also be supplied so the IDENTIFY payload matches the HTTP
        headers sent for the same token.  If omitted a fresh fingerprint is
        generated for this session.
    browser_version:
        Browser version string corresponding to ``user_agent``.
    client_identity:
        Dict with keys ``client_launch_id``, ``launch_signature``, and
        ``client_heartbeat_session_id`` from the token's fingerprint profile.
    locale:
        BCP-47 locale tag (e.g. ``'en-US'``, ``'de'``) used as
        ``system_locale`` in the IDENTIFY payload.
    """

    def __init__(
        self,
        token: str,
        proxy: str | None = None,
        user_agent: str | None = None,
        browser_version: str | None = None,
        client_identity: dict | None = None,
        locale: str = 'en-US',
    ) -> None:
        self.token = token
        self.proxy = proxy

        self.session_id: str | None = None
        self.analytics_token: str | None = None

        self._ready = asyncio.Event()
        self._connected = asyncio.Event()
        self._closing = False
        self._packets_recv = 0
        self._heartbeat_interval: float | None = None

        self._ws = None
        self._http_session = None
        self._heartbeat_task: asyncio.Task | None = None
        self._handle_task: asyncio.Task | None = None

        # Use caller-supplied fingerprint values when provided (so the IDENTIFY
        # payload matches the HTTP headers for the same token), otherwise
        # generate a fresh fingerprint for this session.
        if user_agent is not None:
            self._user_agent: str = user_agent
            _browser_version = browser_version or '0'
            self._client_identity: dict[str, str] = client_identity or {
                k: str(uuid.uuid4())
                for k in ('client_launch_id', 'launch_signature', 'client_heartbeat_session_id')
            }
        else:
            fingerprint = asdict(_FINGERPRINT_GENERATOR.generate(browser='firefox', os='macos'))
            navigator = fingerprint.get('navigator') or {}
            self._user_agent = navigator.get('userAgent') or ''
            _browser_version = '0'
            uda = navigator.get('userAgentData')
            if uda and uda.get('brands'):
                _browser_version = str(uda['brands'][-1].get('version', '0'))
            else:
                m = _FIREFOX_RE.search(self._user_agent)
                if m:
                    _browser_version = m.group(1)
            self._client_identity = {
                k: str(uuid.uuid4())
                for k in ('client_launch_id', 'launch_signature', 'client_heartbeat_session_id')
            }

        self._properties: dict = {
            'os': 'macos',
            'browser': 'firefox',
            'device': '',
            'system_locale': locale,
            'browser_user_agent': self._user_agent,
            'browser_version': _browser_version,
            'os_version': '10',
            'referrer': '',
            'referring_domain': '',
            'referrer_current': '',
            'referring_domain_current': '',
            'release_channel': 'stable',
            'client_event_source': None,
            **self._client_identity,
            'client_app_state': 'focused',
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the WebSocket connection and start the message handler."""
        logger.info('GatewaySession connecting to %s', GATEWAY_URL)
        kwargs: dict = {'impersonate': 'firefox'}
        if self.proxy:
            kwargs['proxies'] = {'https': self.proxy, 'http': self.proxy}

        self._http_session = AsyncSession(**kwargs)
        self._ws = await self._http_session.ws_connect(GATEWAY_URL)
        logger.debug('GatewaySession WebSocket connected')
        self._handle_task = asyncio.create_task(self._handle_messages())

    async def wait_for_ready(self, timeout: float = 20.0) -> bool:
        """Wait until the READY event arrives, or *timeout* seconds pass.

        Returns ``True`` when the gateway session is ready, ``False`` on timeout.
        """
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning('GatewaySession READY timeout after %.1fs', timeout)
            return False

    async def close(self) -> None:
        """Shut down the gateway connection gracefully."""
        if self._closing:
            return
        self._closing = True

        for task in (self._heartbeat_task, self._handle_task):
            if task:
                task.cancel()

        if self._ws:
            try:
                await self._ws.close()
            except Exception:  # pragma: no cover
                pass

        if self._http_session:
            try:
                await self._http_session.close()
            except Exception:  # pragma: no cover
                pass

    # Context-manager helpers

    async def __aenter__(self) -> 'GatewaySession':
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Internal WebSocket message handling
    # ------------------------------------------------------------------

    async def _handle_messages(self) -> None:
        try:
            async for message in self._ws:
                if self._closing:
                    break
                await self._process_raw(message)
        except Exception as exc:
            logger.debug('GatewaySession handler exited: %s', exc)

    async def _process_raw(self, message) -> None:
        if isinstance(message, bytes):
            message = message.decode('utf-8')
        try:
            data = json.loads(message)
        except Exception:
            return

        op = data.get('op')
        if op == 10:  # HELLO
            self._heartbeat_interval = data['d']['heartbeat_interval']
            logger.info(
                'GatewaySession HELLO received heartbeat_interval=%.0fms',
                self._heartbeat_interval,
            )
            await self._send_identify()
            self._heartbeat_task = asyncio.create_task(self._heartbeat())
            self._connected.set()
        elif data.get('t') == 'READY':
            d = data.get('d') or {}
            self.session_id = d.get('session_id')
            self.analytics_token = d.get('analytics_token')
            logger.info(
                'GatewaySession READY received session_id=%s',
                self.session_id,
            )
            self._ready.set()

    async def _send_identify(self) -> None:
        logger.info('GatewaySession sending IDENTIFY')
        p = self._properties
        payload = {
            'op': 2,
            'd': {
                'token': self.token,
                'capabilities': 1734653,
                'properties': {
                    'os': p['os'],
                    'browser': p['browser'],
                    'device': p['device'],
                    'system_locale': p['system_locale'],
                    'browser_user_agent': p['browser_user_agent'],
                    'browser_version': p['browser_version'],
                    'os_version': p['os_version'],
                    'release_channel': p['release_channel'],
                    'client_event_source': None,
                    'client_launch_id': self._client_identity['client_launch_id'],
                    'launch_signature': self._client_identity['launch_signature'],
                    'client_app_state': 'focused',
                    'is_fast_connect': False,
                    'gateway_connect_reasons': 'AppSkeleton',
                },
                'presence': {
                    'status': 'unknown',
                    'since': 0,
                    'activities': [],
                    'afk': False,
                },
                'compress': False,
                'client_state': {'guild_versions': {}},
            },
        }
        await self._ws.send_str(json.dumps(payload))

    async def _heartbeat(self) -> None:
        while not self._closing and self._heartbeat_interval:
            try:
                await asyncio.sleep(self._heartbeat_interval / 1000)
                await self._ws.send_str(json.dumps({'op': 1, 'd': self._packets_recv}))
                self._packets_recv += 1
            except Exception:
                break
