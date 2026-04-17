from app.services.discord_client import DiscordClient


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)
        self.headers = {}

    def json(self):
        return self._payload


class _JoinAsyncClient:
    def __init__(self, *args, **kwargs):
        self.posts = []
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

    async def post(self, url, headers=None, json=None):
        self.posts.append((url, json))
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
            return {'status': 'ready', 'captcha_key': 'solved', 'captcha_rqtoken': 'rq'}

    client.captcha_solver = _Solver()
    monkeypatch.setattr(client, 'validate_guild_access', _ok_access)
    monkeypatch.setattr(client, 'complete_onboarding', _ok_onboarding)

    result = __import__('asyncio').run(
        client.join_guild_via_invite('abc123', 'token-value')
    )

    assert result['status'] == 'joined'
    assert fake_client.posts[1][1]['captcha_key'] == 'solved'


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
