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
    # GatewaySession always uses the firefox/macos profile for IDENTIFY.
    assert props['browser'] == gw._properties['browser']
    assert props['os'] == gw._properties['os']
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

    async def _fake_acquire(self, token, proxy_url=None, timeout=20.0, **kwargs):
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


def test_token_fingerprint_is_consistent():
    """Same token always returns the same fingerprint object."""
    import app.services.discord_client as dc_module
    dc_module._TOKEN_FP_CACHE.clear()
    client = dc_module.DiscordClient()
    fp1 = client._get_token_fingerprint('stable-token')
    fp2 = client._get_token_fingerprint('stable-token')
    assert fp1 is fp2


def test_different_tokens_get_different_fingerprints():
    """Different tokens get independent fingerprint instances."""
    import app.services.discord_client as dc_module
    dc_module._TOKEN_FP_CACHE.clear()
    client = dc_module.DiscordClient()
    fp_a = client._get_token_fingerprint('token-aaa')
    fp_b = client._get_token_fingerprint('token-bbb')
    assert fp_a is not fp_b


def test_locale_is_updated_in_cached_fingerprint():
    """Passing a non-default locale updates the cached fingerprint in place."""
    import app.services.discord_client as dc_module
    dc_module._TOKEN_FP_CACHE.clear()
    client = dc_module.DiscordClient()
    client._get_token_fingerprint('tok-locale')
    fp = client._get_token_fingerprint('tok-locale', locale='de')
    assert fp.locale == 'de'
    # Second call with same locale returns same object.
    assert client._get_token_fingerprint('tok-locale', locale='de') is fp


def test_discord_headers_contains_required_fields():
    """_discord_headers returns all mandatory Discord HTTP header fields."""
    import app.services.discord_client as dc_module
    dc_module._TOKEN_FP_CACHE.clear()
    client = dc_module.DiscordClient()
    headers = client._discord_headers('my-token')
    assert headers['Authorization'] == 'my-token'
    assert 'User-Agent' in headers
    assert 'X-Super-Properties' in headers
    assert headers['X-Discord-Locale'] == 'en-US'
    assert headers['x-debug-options'] == 'bugReporterEnabled'
    assert headers['Origin'] == 'https://discord.com'
    assert 'X-Discord-Timezone' in headers
    assert 'Content-Type' not in headers  # not requested


def test_discord_headers_content_type_flag():
    """content_type=True adds Content-Type: application/json."""
    import app.services.discord_client as dc_module
    client = dc_module.DiscordClient()
    headers = client._discord_headers('tok', content_type=True)
    assert headers.get('Content-Type') == 'application/json'


def test_discord_headers_referer_and_context():
    """Referer and X-Context-Properties are included when provided."""
    import app.services.discord_client as dc_module
    client = dc_module.DiscordClient()
    headers = client._discord_headers(
        'tok',
        referer='https://discord.com/invite/abc',
        context_properties='dGVzdA==',
    )
    assert headers['Referer'] == 'https://discord.com/invite/abc'
    assert headers['X-Context-Properties'] == 'dGVzdA=='


def test_discord_headers_locale_in_accept_language():
    """Non-default locale appears in Accept-Language."""
    import app.services.discord_client as dc_module
    dc_module._TOKEN_FP_CACHE.clear()
    client = dc_module.DiscordClient()
    client._get_token_fingerprint('tok-lang', locale='pt-BR')
    headers = client._discord_headers('tok-lang')
    assert headers['X-Discord-Locale'] == 'pt-BR'
    assert 'pt-BR' in headers['Accept-Language']


def test_gateway_session_uses_provided_fingerprint():
    """GatewaySession stores the provided user_agent and browser_version."""
    import asyncio
    from app.services.gateway_session import GatewaySession
    gw = GatewaySession(
        token='gw-tok',
        user_agent='Mozilla/5.0 TestBrowser/1.0',
        browser_version='1.0',
        client_identity={
            'client_launch_id': 'lid',
            'launch_signature': 'sig',
            'client_heartbeat_session_id': 'hb',
        },
        locale='fr',
    )
    assert gw._user_agent == 'Mozilla/5.0 TestBrowser/1.0'
    assert gw._properties['browser_version'] == '1.0'
    assert gw._properties['system_locale'] == 'fr'
    assert gw._client_identity['client_launch_id'] == 'lid'


def test_send_message_includes_nonce_and_fingerprint_headers(monkeypatch):
    """send_message posts a body with nonce and uses fingerprint headers."""
    import asyncio
    import app.services.discord_client as dc_module

    class _FakePost:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def post(self, url, headers=None, json=None):
            self.last_headers = headers
            self.last_json = json
            r = type('R', (), {})()
            r.status_code = 200
            r.text = '{}'
            r.json = lambda: {'id': 'm1'}
            r.content = b'{}'
            r.headers = {}
            return r

    fake = _FakePost()
    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', lambda **kw: fake)
    client = dc_module.DiscordClient()
    result = asyncio.run(client.send_message('ch1', 'hello', 'tok'))
    assert result['status'] == 'sent'
    assert 'nonce' in fake.last_json
    assert fake.last_json['mobile_network_type'] == 'unknown'
    assert fake.last_headers.get('User-Agent') is not None
    assert fake.last_headers.get('x-debug-options') == 'bugReporterEnabled'


def test_add_friend_sends_put_request(monkeypatch):
    """add_friend issues a PUT to /users/@me/relationships/{user_id}."""
    import asyncio
    import app.services.discord_client as dc_module

    class _FakePut:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def put(self, url, headers=None, json=None):
            self.last_url = url
            self.last_headers = headers
            r = type('R', (), {})()
            r.status_code = 204
            r.text = ''
            r.json = lambda: {}
            r.headers = {}
            return r

    fake = _FakePut()
    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', lambda **kw: fake)
    client = dc_module.DiscordClient()
    result = asyncio.run(client.add_friend('user-123', 'tok-add'))
    assert result['status'] == 'sent'
    assert 'relationships/user-123' in fake.last_url
    assert fake.last_headers.get('x-debug-options') == 'bugReporterEnabled'


def test_add_friend_uses_anysolver_on_captcha(monkeypatch):
    """add_friend calls AnySolver and retries the PUT when captcha is required."""
    import asyncio
    import app.services.discord_client as dc_module

    responses = [
        {'status_code': 400, 'payload': {'captcha_sitekey': 'sk', 'captcha_rqdata': 'rq', 'captcha_rqtoken': 'rqt'}},
        {'status_code': 204, 'payload': {}},
    ]

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def put(self, url, headers=None, json=None):
            resp_data = responses.pop(0)
            r = type('R', (), {})()
            r.status_code = resp_data['status_code']
            r.text = str(resp_data['payload'])
            r.json = lambda: resp_data['payload']
            r.headers = {}
            return r

    class _Solver:
        is_enabled = True
        @staticmethod
        def is_captcha_challenge(p): return bool(p.get('captcha_sitekey'))
        async def solve_discord_challenge(self, *a, **kw):
            return {'status': 'ready', 'captcha_key': 'solved-key', 'captcha_rqtoken': 'rqt2', 'captcha_rqdata': 'rq2'}

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', lambda **kw: _FakeClient())
    client = dc_module.DiscordClient()
    client.captcha_solver = _Solver()
    result = asyncio.run(client.add_friend('u1', 'tok'))
    assert result['status'] == 'sent'


def test_open_dm_channel_posts_recipient_id(monkeypatch):
    """open_dm_channel POSTs recipient_id to /users/@me/channels."""
    import asyncio
    import app.services.discord_client as dc_module

    class _FakePost:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def post(self, url, headers=None, json=None):
            self.last_url = url
            self.last_json = json
            r = type('R', (), {})()
            r.status_code = 200
            r.json = lambda: {'id': 'dm-ch-1', 'type': 1}
            r.text = '{}'
            r.headers = {}
            return r

    fake = _FakePost()
    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', lambda **kw: fake)
    client = dc_module.DiscordClient()
    result = asyncio.run(client.open_dm_channel('user-456', 'tok-dm'))
    assert result['status'] == 'ok'
    assert 'dm-ch-1' in str(result['channel'])
    assert fake.last_json == {'recipient_id': 'user-456'}


def test_leave_guild_sends_delete(monkeypatch):
    """leave_guild issues a DELETE to /users/@me/guilds/{guild_id}."""
    import asyncio
    import app.services.discord_client as dc_module

    class _FakeDelete:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def delete(self, url, headers=None, json=None):
            self.last_url = url
            r = type('R', (), {})()
            r.status_code = 204
            r.text = ''
            r.json = lambda: {}
            r.headers = {}
            return r

    fake = _FakeDelete()
    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', lambda **kw: fake)
    client = dc_module.DiscordClient()
    result = asyncio.run(client.leave_guild('guild-99', 'tok-leave'))
    assert result['status'] == 'left'
    assert 'guilds/guild-99' in fake.last_url
