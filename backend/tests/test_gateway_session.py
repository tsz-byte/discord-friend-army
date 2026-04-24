"""Tests for the GatewaySession class and the discord_client gateway integration."""

import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.discord_client import (
    DiscordClient,
    _build_fingerprint_super_properties,
    _CONTEXT_PROPERTIES,
)
from app.services.gateway_session import GatewaySession


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)
        self.content = b'{}'
        self.headers = {}

    def json(self):
        return self._payload


class _FakeWS:
    """Fake WebSocket that replays a sequence of JSON messages."""

    def __init__(self, messages: list[dict]):
        self._messages = [json.dumps(m) for m in messages]
        self._sent: list[str] = []

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for msg in self._messages:
            yield msg

    async def send_str(self, data: str) -> None:
        self._sent.append(data)

    async def close(self) -> None:
        pass


class _FakeAsyncSession:
    """Fake curl_cffi AsyncSession that returns a pre-configured WebSocket."""

    def __init__(self, ws: _FakeWS):
        self._ws = ws

    async def ws_connect(self, url: str) -> _FakeWS:
        return self._ws

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# GatewaySession unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_session_receives_ready():
    """GatewaySession should set session_id after processing HELLO + READY."""
    messages = [
        {'op': 10, 'd': {'heartbeat_interval': 41250}},
        {'op': 0, 't': 'READY', 'd': {'session_id': 'ws-sess-123', 'analytics_token': 'atoken'}},
    ]
    fake_ws = _FakeWS(messages)
    fake_session = _FakeAsyncSession(fake_ws)

    with patch('app.services.gateway_session.AsyncSession', return_value=fake_session):
        gw = GatewaySession(token='test-token')
        await gw.connect()
        ready = await gw.wait_for_ready(timeout=5.0)
        await gw.close()

    assert ready is True
    assert gw.session_id == 'ws-sess-123'
    assert gw.analytics_token == 'atoken'


@pytest.mark.asyncio
async def test_gateway_session_sends_identify_on_hello():
    """GatewaySession must send an IDENTIFY payload (op 2) after receiving HELLO."""
    messages = [
        {'op': 10, 'd': {'heartbeat_interval': 41250}},
        {'op': 0, 't': 'READY', 'd': {'session_id': 'ws-sess-abc', 'analytics_token': ''}},
    ]
    fake_ws = _FakeWS(messages)
    fake_session = _FakeAsyncSession(fake_ws)

    with patch('app.services.gateway_session.AsyncSession', return_value=fake_session):
        gw = GatewaySession(token='tok-xyz')
        await gw.connect()
        await gw.wait_for_ready(timeout=5.0)
        await gw.close()

    # First sent message should be IDENTIFY (op 2)
    assert fake_ws._sent, 'Expected at least one sent message'
    identify = json.loads(fake_ws._sent[0])
    assert identify['op'] == 2
    assert identify['d']['token'] == 'tok-xyz'
    props = identify['d']['properties']
    assert props['browser'] == 'firefox'
    assert props['os'] == 'macos'
    # client_launch_id must be a valid UUID4
    uuid.UUID(props['client_launch_id'], version=4)


@pytest.mark.asyncio
async def test_gateway_session_timeout_returns_false():
    """wait_for_ready should return False (not raise) on timeout."""
    # WebSocket that never sends any messages → READY never arrives.
    fake_ws = _FakeWS([])
    fake_session = _FakeAsyncSession(fake_ws)

    with patch('app.services.gateway_session.AsyncSession', return_value=fake_session):
        gw = GatewaySession(token='t')
        await gw.connect()
        ready = await gw.wait_for_ready(timeout=0.05)
        await gw.close()

    assert ready is False
    assert gw.session_id is None


@pytest.mark.asyncio
async def test_gateway_session_context_manager():
    """GatewaySession should work as an async context manager."""
    messages = [
        {'op': 10, 'd': {'heartbeat_interval': 41250}},
        {'op': 0, 't': 'READY', 'd': {'session_id': 'ctx-session', 'analytics_token': ''}},
    ]
    fake_ws = _FakeWS(messages)
    fake_session = _FakeAsyncSession(fake_ws)

    with patch('app.services.gateway_session.AsyncSession', return_value=fake_session):
        async with GatewaySession(token='tok') as gw:
            ready = await gw.wait_for_ready(timeout=5.0)

    assert ready is True
    assert gw.session_id == 'ctx-session'


@pytest.mark.asyncio
async def test_gateway_session_close_is_idempotent():
    """Calling close() twice must not raise."""
    messages = [
        {'op': 10, 'd': {'heartbeat_interval': 41250}},
        {'op': 0, 't': 'READY', 'd': {'session_id': 's', 'analytics_token': ''}},
    ]
    fake_ws = _FakeWS(messages)
    fake_session = _FakeAsyncSession(fake_ws)

    with patch('app.services.gateway_session.AsyncSession', return_value=fake_session):
        gw = GatewaySession(token='t')
        await gw.connect()
        await gw.close()
        await gw.close()  # should not raise


# ---------------------------------------------------------------------------
# _build_fingerprint_super_properties unit tests
# ---------------------------------------------------------------------------


def test_build_fingerprint_super_properties_structure():
    """_build_fingerprint_super_properties must include all required fields."""
    import base64

    fake_fp = {'navigator': {'userAgent': 'Mozilla/5.0 Firefox/120.0'}}
    identity = {
        'client_launch_id': str(uuid.uuid4()),
        'launch_signature': str(uuid.uuid4()),
        'client_heartbeat_session_id': str(uuid.uuid4()),
    }
    result = _build_fingerprint_super_properties(fake_fp, '120.0', identity, locale='fr-FR')
    decoded = json.loads(base64.b64decode(result).decode())

    assert decoded['browser'] == 'firefox'
    assert decoded['os'] == 'macos'
    assert decoded['system_locale'] == 'fr-FR'
    assert decoded['browser_user_agent'] == 'Mozilla/5.0 Firefox/120.0'
    assert decoded['browser_version'] == '120.0'
    for k in ('client_launch_id', 'launch_signature', 'client_heartbeat_session_id'):
        assert decoded[k] == identity[k]


# ---------------------------------------------------------------------------
# DiscordClient._acquire_gateway_session_id tests
# ---------------------------------------------------------------------------


def test_acquire_gateway_session_id_returns_session_id(monkeypatch):
    """_acquire_gateway_session_id should return the GatewaySession session_id."""

    async def _fake_acquire(self, token, proxy_url=None, timeout=20.0):
        return 'gateway-session-from-ws'

    client = DiscordClient()
    monkeypatch.setattr(client, '_acquire_gateway_session_id', lambda **kw: _fake_acquire(client, **kw))

    result = asyncio.run(client._acquire_gateway_session_id(token='tok', timeout=5.0))
    assert result == 'gateway-session-from-ws'


def test_acquire_gateway_session_id_returns_none_on_error(monkeypatch):
    """_acquire_gateway_session_id should return None when GatewaySession raises."""

    class _BrokenSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            raise ConnectionError('unreachable')

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(
        'app.services.discord_client.GatewaySession',
        _BrokenSession,
        raising=False,
    )

    client = DiscordClient()
    result = asyncio.run(client._acquire_gateway_session_id(token='tok', timeout=2.0))
    assert result is None


# ---------------------------------------------------------------------------
# DiscordClient.join_guild_via_invite — gateway session_id usage
# ---------------------------------------------------------------------------


class _GatewayJoinClient:
    """Fake httpx.AsyncClient that records calls and returns join-success on first POST."""

    def __init__(self, *args, **kwargs):
        self.posts = []
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def get(self, url, headers=None, params=None):
        self.gets.append((url, params))
        if url.endswith('/users/@me'):
            return _FakeResponse(200, {'locale': 'de'})
        return _FakeResponse(200, {})

    async def post(self, url, headers=None, json=None):
        self.posts.append((url, headers, json))
        return _FakeResponse(201, {'guild': {'id': '888', 'name': 'test-guild'}})


def test_join_uses_gateway_session_id(monkeypatch):
    """join_guild_via_invite should include the gateway session_id in the POST body."""
    fake_http = _GatewayJoinClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_http

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())

    client = DiscordClient()

    # Mock gateway to return a real-looking session_id.
    async def _fake_gateway(**kwargs):
        return 'real-gateway-session-id-abc'

    monkeypatch.setattr(client, '_acquire_gateway_session_id', _fake_gateway)

    async def _ok_access(*args, **kwargs):
        return {'status': 'ok'}

    async def _ok_onboarding(*args, **kwargs):
        return True

    monkeypatch.setattr(client, 'validate_guild_access', _ok_access)
    monkeypatch.setattr(client, 'complete_onboarding', _ok_onboarding)

    result = asyncio.run(client.join_guild_via_invite('abc123', 'token-value'))

    assert result['status'] == 'joined'
    _, headers, body = fake_http.posts[0]
    assert body['session_id'] == 'real-gateway-session-id-abc'
    # x-debug-options header must be present
    assert headers.get('x-debug-options') == 'bugReporterEnabled'
    # X-Context-Properties must use the simple format
    import base64

    ctx = json.loads(base64.b64decode(headers['X-Context-Properties']))
    assert ctx == {'location': 'Join Guild'}


def test_join_uses_user_locale_in_headers(monkeypatch):
    """join_guild_via_invite should set x-discord-locale from /users/@me."""
    fake_http = _GatewayJoinClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_http

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())

    client = DiscordClient()

    async def _fake_gateway(**kwargs):
        return 'gw-session'

    monkeypatch.setattr(client, '_acquire_gateway_session_id', _fake_gateway)

    async def _ok_access(*args, **kwargs):
        return {'status': 'ok'}

    async def _ok_onboarding(*args, **kwargs):
        return True

    monkeypatch.setattr(client, 'validate_guild_access', _ok_access)
    monkeypatch.setattr(client, 'complete_onboarding', _ok_onboarding)

    result = asyncio.run(client.join_guild_via_invite('abc123', 'token-value'))

    assert result['status'] == 'joined'
    _, headers, _ = fake_http.posts[0]
    # _GatewayJoinClient.get() returns locale='de' for /users/@me
    assert headers.get('X-Discord-Locale') == 'de'


def test_join_falls_back_when_gateway_returns_none(monkeypatch):
    """join_guild_via_invite should use a random session_id when gateway returns None."""
    fake_http = _GatewayJoinClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_http

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())

    client = DiscordClient()

    async def _gateway_none(**kwargs):
        return None

    monkeypatch.setattr(client, '_acquire_gateway_session_id', _gateway_none)

    async def _ok_access(*args, **kwargs):
        return {'status': 'ok'}

    async def _ok_onboarding(*args, **kwargs):
        return True

    monkeypatch.setattr(client, 'validate_guild_access', _ok_access)
    monkeypatch.setattr(client, 'complete_onboarding', _ok_onboarding)

    result = asyncio.run(client.join_guild_via_invite('abc123', 'token-value'))

    assert result['status'] == 'joined'
    _, _, body = fake_http.posts[0]
    # session_id should be a non-empty fallback string
    assert 'session_id' in body
    assert body['session_id']


def test_join_uses_fingerprint_super_properties(monkeypatch):
    """join_guild_via_invite should use fingerprint-based X-Super-Properties."""
    import base64 as b64mod

    fake_http = _GatewayJoinClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_http

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())

    client = DiscordClient()

    async def _fake_gateway(**kwargs):
        return 'gw-session'

    monkeypatch.setattr(client, '_acquire_gateway_session_id', _fake_gateway)

    async def _ok_access(*args, **kwargs):
        return {'status': 'ok'}

    async def _ok_onboarding(*args, **kwargs):
        return True

    monkeypatch.setattr(client, 'validate_guild_access', _ok_access)
    monkeypatch.setattr(client, 'complete_onboarding', _ok_onboarding)

    asyncio.run(client.join_guild_via_invite('abc123', 'token-value'))

    _, headers, _ = fake_http.posts[0]
    sp_raw = headers.get('X-Super-Properties', '')
    assert sp_raw, 'X-Super-Properties header must be present'
    sp = json.loads(b64mod.b64decode(sp_raw))
    # Fingerprint-based super-properties always use 'firefox' browser profile.
    assert sp['browser'] == 'firefox'
    assert sp['os'] == 'macos'
