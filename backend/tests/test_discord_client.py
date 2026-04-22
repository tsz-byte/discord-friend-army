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
    assert fake_client.posts[1][2]['captcha_key'] == 'solved'
    assert fake_client.posts[1][2]['captcha_rqtoken'] == 'rq'
    assert fake_client.posts[1][2]['captcha_rqdata'] == 'rq-data'
    assert fake_client.posts[1][2]['captcha_session_id'] == 'captcha-session-1'
    assert fake_client.posts[1][1]['X-Captcha-Key'] == 'solved'


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
    # Each retry body includes the captcha fields plus a session_id; check only
    # that the captcha keys are present and correct (session_id is randomly generated).
    for _, post_headers, post_payload in (fake_client.posts[1], fake_client.posts[2]):
        for key, val in expected_payload.items():
            assert post_payload.get(key) == val
        assert post_headers.get('X-Captcha-Key') == 'solved'


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
