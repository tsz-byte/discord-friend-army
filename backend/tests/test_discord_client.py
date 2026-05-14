from app.services.discord_client import DiscordClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.models.research import CaptchaChallenge


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)
        self.content = b'{}'
        self.headers = {}

    def json(self):
        return self._payload


class _JoinAsyncClient:
    def __init__(self, *args, **kwargs):
        self.posts = []
        self.gets = []
        self._responses = [
            _FakeResponse(
                400,
                {
                    'captcha_sitekey': 'site-key',
                    'captcha_rqdata': 'rq-data',
                    'captcha_service': 'hcaptcha',
                },
            ),
            _FakeResponse(201, {'guild': {'id': '999', 'name': 'guild'}}),
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None, params=None):
        self.gets.append((url, params))
        return _FakeResponse(200, {})

    async def post(self, url, headers=None, json=None):
        self.posts.append((url, headers, json))
        return self._responses.pop(0)


class _JoinTwoCaptchaAsyncClient:
    def __init__(self, *args, **kwargs):
        self.posts = []
        self.gets = []
        self._responses = [
            _FakeResponse(
                400,
                {
                    'captcha_sitekey': 'site-key',
                    'captcha_rqdata': 'rq-data',
                    'captcha_service': 'hcaptcha',
                },
            ),
            _FakeResponse(
                400,
                {
                    'captcha_sitekey': 'site-key',
                    'captcha_rqdata': 'rq-data',
                    'captcha_service': 'hcaptcha',
                },
            ),
            _FakeResponse(201, {'guild': {'id': '999', 'name': 'guild'}}),
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None, params=None):
        self.gets.append((url, params))
        return _FakeResponse(200, {})

    async def post(self, url, headers=None, json=None):
        self.posts.append((url, headers, json))
        return self._responses.pop(0)


class _JoinNoCaptchaAsyncClient:
    def __init__(self, *args, **kwargs):
        self.posts = []
        self.gets = []
        self._responses = [
            _FakeResponse(
                400,
                {
                    'message': 'Missing Access',
                    'code': 50001,
                },
            ),
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None, params=None):
        self.gets.append((url, params))
        return _FakeResponse(200, {})

    async def post(self, url, headers=None, json=None):
        self.posts.append((url, headers, json))
        return self._responses.pop(0)


class _PatchAsyncClient:
    def __init__(self, *args, **kwargs):
        self.last_url = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def patch(self, url, headers=None, json=None):
        self.last_url = url
        return _FakeResponse(204, {})


class _WebhookAsyncClient:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self._list_payload = [{'id': '11', 'token': 'hook-token', 'type': 1, 'name': 'DFA Mirror'}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None, params=None):
        self.calls.append(('GET', url, None))
        return _FakeResponse(200, self._list_payload)

    async def post(self, url, headers=None, json=None):
        self.calls.append(('POST', url, json))
        if '/channels/' in url and '/webhooks' in url:
            return _FakeResponse(201, {'id': '11', 'token': 'hook-token', 'type': 1, 'name': 'DFA Mirror'})
        return _FakeResponse(200, {'id': 'm1'})


class _SendEmbedAsyncClient:
    def __init__(self, *args, **kwargs):
        self.last_json = None
        self.last_headers = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        self.last_json = json
        self.last_headers = headers
        return _FakeResponse(200, {'id': 'msg-1'})


def test_join_uses_captcha_solution(monkeypatch):
    fake_client = _JoinAsyncClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_client

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())

    client = DiscordClient()

    async def _ok_access(*args, **kwargs):
        return {'status': 'ok'}

    async def _ok_onboarding(*args, **kwargs):
        return True

    class _Solver:
        is_enabled = True

        @staticmethod
        def is_captcha_challenge(payload):
            return True

        async def solve_discord_challenge(self, *args, **kwargs):
            return {
                'status': 'ready',
                'captcha_key': 'solved',
                'captcha_rqtoken': 'rq',
                'captcha_rqdata': 'rq-data',
                'captcha_session_id': 'captcha-session-1',
            }

    client.captcha_solver = _Solver()
    monkeypatch.setattr(client, 'validate_guild_access', _ok_access)
    monkeypatch.setattr(client, 'complete_onboarding', _ok_onboarding)

    result = __import__('asyncio').run(
        client.join_guild_via_invite('abc123', 'token-value')
    )

    assert result['status'] == 'joined'
    assert fake_client.gets, 'Expected invite preflight GET call'
    assert fake_client.gets[0][1] == {
        'with_counts': 'true',
        'with_expiration': 'true',
        'with_permissions': 'true',
    }
    # Captcha fields must be in HTTP headers only — not merged into the request body.
    _, retry_headers, retry_body = fake_client.posts[1]
    assert list(retry_body.keys()) == ['session_id'], (
        'Request body must contain only session_id; captcha fields must go in headers'
    )
    assert 'captcha_key' not in retry_body
    # All four captcha headers must be present.
    assert retry_headers['X-Captcha-Key'] == 'solved'
    assert retry_headers['X-Captcha-Rqtoken'] == 'rq'
    assert retry_headers['X-Captcha-Rqdata'] == 'rq-data'
    assert retry_headers['X-Captcha-Session-Id'] == 'captcha-session-1'


def test_join_retries_captcha_solve_on_second_challenge(monkeypatch):
    fake_client = _JoinTwoCaptchaAsyncClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_client

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())

    client = DiscordClient()

    async def _ok_access(*args, **kwargs):
        return {'status': 'ok'}

    async def _ok_onboarding(*args, **kwargs):
        return True

    class _Solver:
        is_enabled = True

        @staticmethod
        def is_captcha_challenge(payload):
            return bool(payload.get('captcha_sitekey'))

        async def solve_discord_challenge(self, *args, **kwargs):
            return {
                'status': 'ready',
                'captcha_key': 'solved',
                'captcha_rqtoken': 'rq',
                'captcha_rqdata': 'rq-data',
                'captcha_context_id': 'ctx-1',
                'captcha_context_id_empty': False,
                'captcha_ua': 'ua-1',
                'captcha_lang': 'en-US',
                'task_id': 'task-1',
            }

    client.captcha_solver = _Solver()
    monkeypatch.setattr(client, 'validate_guild_access', _ok_access)
    monkeypatch.setattr(client, 'complete_onboarding', _ok_onboarding)

    result = __import__('asyncio').run(
        client.join_guild_via_invite('abc123', 'token-value')
    )

    expected_payload = {
        'captcha_key': 'solved',
        'captcha_rqtoken': 'rq',
        'captcha_rqdata': 'rq-data',
    }
    assert result['status'] == 'joined'
    # First captcha retry (posts[1]) uses variant 0 — all captcha fields in headers,
    # body contains only session_id.
    _, hdrs1, body1 = fake_client.posts[1]
    assert list(body1.keys()) == ['session_id'], 'Body must only contain session_id on captcha retry'
    assert hdrs1['X-Captcha-Key'] == 'solved'
    assert hdrs1['X-Captcha-Rqtoken'] == 'rq'
    assert hdrs1['X-Captcha-Rqdata'] == 'rq-data'
    # Second captcha retry (posts[2]) falls back to variant 1 (no rqdata) without
    # re-solving — the multi-variant strategy avoids an extra AnySolver round-trip.
    _, hdrs2, body2 = fake_client.posts[2]
    assert list(body2.keys()) == ['session_id'], 'Body must only contain session_id on second captcha retry'
    assert hdrs2['X-Captcha-Key'] == 'solved'
    assert hdrs2['X-Captcha-Rqtoken'] == 'rq'
    assert 'X-Captcha-Rqdata' not in hdrs2, 'Variant 1 must omit rqdata'


def test_join_uses_captcha_solver_only_for_captcha_challenges(monkeypatch):
    fake_client = _JoinNoCaptchaAsyncClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_client

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())
    client = DiscordClient()

    class _Solver:
        is_enabled = True
        called = False

        @staticmethod
        def is_captcha_challenge(payload):
            return False

        async def solve_discord_challenge(self, *args, **kwargs):
            self.called = True
            return {'status': 'ready', 'captcha_key': 'should-not-be-used'}

    solver = _Solver()
    client.captcha_solver = solver

    result = __import__('asyncio').run(client.join_guild_via_invite('abc123', 'token-value'))

    assert result['status'] == 'failed'
    assert solver.called is False


def test_join_marks_retry_when_context_is_empty(monkeypatch):
    fake_client = _JoinAsyncClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_client

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())
    client = DiscordClient()

    engine = create_engine('sqlite:///:memory:', future=True)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    row = CaptchaChallenge(task_id='task-empty', solver_status='ready', captcha_context_id_empty=True)
    db.add(row)
    db.commit()

    async def _ok_access(*args, **kwargs):
        return {'status': 'ok'}

    async def _ok_onboarding(*args, **kwargs):
        return True

    class _Solver:
        is_enabled = True

        @staticmethod
        def is_captcha_challenge(payload):
            return True

        async def solve_discord_challenge(self, *args, **kwargs):
            return {
                'status': 'ready',
                'captcha_key': 'solved',
                'captcha_rqtoken': 'rq',
                'captcha_context_id_empty': True,
                'task_id': 'task-empty',
            }

    client.captcha_solver = _Solver()
    monkeypatch.setattr(client, 'validate_guild_access', _ok_access)
    monkeypatch.setattr(client, 'complete_onboarding', _ok_onboarding)

    result = __import__('asyncio').run(
        client.join_guild_via_invite('abc123', 'token-value', db=db)
    )

    db.refresh(row)
    assert result['status'] == 'joined'
    assert row.retried_with_empty_context is True
    db.close()


def test_patch_nickname_uses_members_me(monkeypatch):
    fake_client = _PatchAsyncClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_client

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())
    client = DiscordClient()

    result = __import__('asyncio').run(
        client.patch_member_nickname('123', 'any-user', 'nick', 'token')
    )

    assert result['status'] == 'updated'
    assert fake_client.last_url.endswith('/guilds/123/members/@me')

    __import__('asyncio').run(
        client.patch_member_nickname('123', 'different-user', 'nick', 'token')
    )
    assert fake_client.last_url.endswith('/guilds/123/members/@me')


def test_get_or_create_channel_webhook_reuses_existing(monkeypatch):
    fake_client = _WebhookAsyncClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_client

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())
    client = DiscordClient()
    result = __import__('asyncio').run(client.get_or_create_channel_webhook('999', bot_token='bot-xyz'))
    assert result['status'] == 'ok'
    assert result['webhook_id'] == '11'
    # list endpoint called, create endpoint not needed
    assert any(call[0] == 'GET' for call in fake_client.calls)
    assert not any(call[0] == 'POST' and '/channels/999/webhooks' in call[1] for call in fake_client.calls)


def test_send_webhook_message_sets_username_avatar_and_timestamp(monkeypatch):
    fake_client = _WebhookAsyncClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_client

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())
    client = DiscordClient()
    result = __import__('asyncio').run(
        client.send_webhook_message(
            channel_id='999',
            content='hello world',
            username='alice',
            avatar_url='https://cdn.example/avatar.png',
            timestamp_iso='2026-01-01T00:00:00.000Z',
            bot_token='bot-xyz',
        )
    )
    assert result['status'] == 'sent'
    send_calls = [c for c in fake_client.calls if c[0] == 'POST' and '/webhooks/11/hook-token?wait=true' in c[1]]
    assert send_calls, 'Expected webhook send call not found'
    send_call = send_calls[0]
    payload = send_call[2]
    assert payload['username'] == 'alice'
    assert payload['avatar_url'] == 'https://cdn.example/avatar.png'
    assert payload['content'].startswith('[2026-01-01T00:00:00.000Z] hello world')


def test_build_captcha_payload_variants_multiple():
    """_build_captcha_payload_variants returns multiple ordered variants."""
    solve_result = {
        'status': 'ready',
        'captcha_key': 'solved-key',
        'captcha_rqtoken': 'rq-token',
        'captcha_rqdata': 'rq-data',
        'captcha_session_id': 'sess-1',
    }
    variants = DiscordClient._build_captcha_payload_variants(solve_result)

    # At least 2 variants expected when all fields are present.
    assert len(variants) >= 2, 'Multiple variants required for retry fallback strategies'

    # Variant 0: most complete — must include key, rqtoken, rqdata, session_id.
    v0 = variants[0]
    assert v0['captcha_key'] == 'solved-key'
    assert v0['captcha_rqtoken'] == 'rq-token'
    assert v0['captcha_rqdata'] == 'rq-data'
    assert v0['captcha_session_id'] == 'sess-1'

    # Variant 1: without rqdata.
    v1 = variants[1]
    assert v1['captcha_key'] == 'solved-key'
    assert v1.get('captcha_rqtoken') == 'rq-token'
    assert 'captcha_rqdata' not in v1

    # No captcha fields ever include status or non-captcha keys.
    for v in variants:
        assert 'status' not in v


def test_build_captcha_payload_variants_key_only():
    """_build_captcha_payload_variants with only captcha_key returns a single variant."""
    solve_result = {
        'status': 'ready',
        'captcha_key': 'only-key',
    }
    variants = DiscordClient._build_captcha_payload_variants(solve_result)
    assert len(variants) == 1
    assert variants[0] == {'captcha_key': 'only-key'}


def test_join_challenge_detection_with_empty_metadata(monkeypatch):
    """Captcha challenge is detected from error_payload even when invite_metadata is empty.

    Simulates a metadata fetch failure (GET returns {}) — the join response
    error_payload must still trigger AnySolver.
    """
    fake_client = _JoinAsyncClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_client

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())
    client = DiscordClient()

    async def _ok_access(*args, **kwargs):
        return {'status': 'ok'}

    async def _ok_onboarding(*args, **kwargs):
        return True

    class _Solver:
        is_enabled = True
        solve_called_with = None

        def is_captcha_challenge(self, payload):
            return bool(payload.get('captcha_sitekey'))

        async def solve_discord_challenge(self, challenge, **kwargs):
            _Solver.solve_called_with = dict(challenge)
            return {
                'status': 'ready',
                'captcha_key': 'solved',
                'captcha_rqtoken': 'rq',
            }

    client.captcha_solver = _Solver()
    monkeypatch.setattr(client, 'validate_guild_access', _ok_access)
    monkeypatch.setattr(client, 'complete_onboarding', _ok_onboarding)

    # GET returns {} (empty metadata — simulates 404/timeout fallback).
    # First POST returns 400 with captcha fields; second returns 201.
    result = __import__('asyncio').run(
        client.join_guild_via_invite('abc123', 'token-value')
    )

    assert result['status'] == 'joined'
    # Solver must have been called with fields from the error_payload.
    assert _Solver.solve_called_with is not None, 'Solver should have been called'
    assert _Solver.solve_called_with.get('captcha_sitekey') == 'site-key'


def test_join_session_id_fallback_not_from_metadata(monkeypatch):
    """Gateway session_id fallback uses random hex, not captcha_session_id from invite_metadata.

    When _acquire_gateway_session_id returns None, the body's session_id must be
    a random 32-char hex string — never the captcha_session_id field from invite metadata.
    """
    fake_client = _JoinAsyncClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_client

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())
    client = DiscordClient()

    async def _ok_access(*args, **kwargs):
        return {'status': 'ok'}

    async def _ok_onboarding(*args, **kwargs):
        return True

    class _Solver:
        is_enabled = True

        @staticmethod
        def is_captcha_challenge(payload):
            return True

        async def solve_discord_challenge(self, *args, **kwargs):
            return {
                'status': 'ready',
                'captcha_key': 'solved',
                'captcha_rqtoken': 'rq',
            }

    client.captcha_solver = _Solver()
    monkeypatch.setattr(client, 'validate_guild_access', _ok_access)
    monkeypatch.setattr(client, 'complete_onboarding', _ok_onboarding)

    # Force gateway to return None (simulates timeout/failure).
    async def _no_gateway(*args, **kwargs):
        return None

    monkeypatch.setattr(client, '_acquire_gateway_session_id', _no_gateway)

    # Provide invite_metadata with a captcha_session_id to ensure it's NOT used as
    # the gateway session_id in the body.
    async def _metadata_with_captcha_session(*args, **kwargs):
        return {'captcha_session_id': 'metadata-captcha-sess', 'captcha_sitekey': 'sk'}

    monkeypatch.setattr(client, '_fetch_invite_metadata', _metadata_with_captcha_session)

    result = __import__('asyncio').run(
        client.join_guild_via_invite('abc123', 'token-value')
    )

    assert result['status'] == 'joined'
    # Body's session_id must NOT be the metadata captcha_session_id.
    first_body = fake_client.posts[0][2]
    assert first_body.get('session_id') != 'metadata-captcha-sess', (
        'session_id in body must not be taken from invite_metadata.captcha_session_id'
    )
    # Must be a random 32-char lowercase hex string.
    sid = first_body.get('session_id', '')
    assert len(sid) == 32 and all(c in '0123456789abcdef' for c in sid), (
        f'Expected random 32-char hex session_id, got: {sid!r}'
    )


def test_send_embed_uses_passed_params(monkeypatch):
    fake_client = _SendEmbedAsyncClient()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return fake_client

    monkeypatch.setattr('app.services.discord_client.httpx.AsyncClient', _Factory())
    client = DiscordClient()

    result = __import__('asyncio').run(
        client.send_embed(
            channel_id='123',
            token='user.token.value',
            content='hello @everyone',
            title='Custom Title',
            description='Custom Description',
            color=16711680,
            fields=[{'name': 'Key', 'value': 'Value', 'inline': True}],
        )
    )

    assert result['status'] == 'sent'
    assert fake_client.last_json['content'] == 'hello @everyone'
    assert fake_client.last_json['embeds'][0]['title'] == 'Custom Title'
    assert fake_client.last_json['embeds'][0]['description'] == 'Custom Description'
    assert fake_client.last_json['embeds'][0]['color'] == 16711680
    assert fake_client.last_json['allowed_mentions']['parse'] == ['everyone']
