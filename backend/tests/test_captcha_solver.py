import asyncio

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.db.session import Base
from app.models.research import CaptchaChallenge
from app.services.captcha_solver import CaptchaSolverService


class _FakeResponse:
    def __init__(self, payload, status_code=200, text=''):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                'http error',
                request=httpx.Request('POST', 'https://example.com'),
                response=httpx.Response(self.status_code),
            )


class _FakeAsyncClient:
    """Simulates AnySolver API for unit tests."""

    def __init__(self, *args, **kwargs):
        self.verify = kwargs.get('verify')
        self.timeout = kwargs.get('timeout')
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, data=None, params=None):
        self.posts.append({'url': url, 'json': json})
        if 'createTask' in url:
            task_type = (json or {}).get('task', {}).get('type')
            if task_type == 'PopularPlatformSessionAction':
                return _FakeResponse({'errorId': 0, 'taskId': 'session-task-123'})
            return _FakeResponse({'errorId': 0, 'taskId': 'captcha-task-123'})
        if 'getTaskResult' in url:
            task_id = (json or {}).get('taskId')
            if task_id == 'session-task-123':
                return _FakeResponse({
                    'errorId': 0,
                    'status': 'ready',
                    'solution': {
                        'sessionId': 'anysolver-session-123',
                        'userAgent': 'anysolver-user-agent',
                    },
                })
            return _FakeResponse({
                'errorId': 0,
                'status': 'ready',
                'cost': '0.0025',
                'solution': {'token': 'solved-token', 'rqtoken': 'rq-token'},
            })
        return _FakeResponse({'errorId': 1, 'errorDescription': 'unknown endpoint'}, status_code=400)


class _ProcessingThenReadyClient(_FakeAsyncClient):
    """Returns 'processing' on first poll, then 'ready'."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._get_task_result_calls = 0

    async def post(self, url, json=None, data=None, params=None):
        self.posts.append({'url': url, 'json': json})
        if 'createTask' in url:
            task_type = (json or {}).get('task', {}).get('type')
            if task_type == 'PopularPlatformSessionAction':
                return _FakeResponse({'errorId': 0, 'taskId': 'session-task-456'})
            return _FakeResponse({'errorId': 0, 'taskId': 'captcha-task-456'})
        if 'getTaskResult' in url:
            task_id = (json or {}).get('taskId')
            if task_id == 'session-task-456':
                return _FakeResponse({
                    'errorId': 0,
                    'status': 'ready',
                    'solution': {'sessionId': 'anysolver-session-456', 'userAgent': 'ua-from-session'},
                })
            self._get_task_result_calls += 1
            if self._get_task_result_calls == 1:
                return _FakeResponse({'errorId': 0, 'status': 'processing'})
            return _FakeResponse({
                'errorId': 0,
                'status': 'ready',
                'cost': '0.003',
                'solution': {'token': 'late-token', 'rqtoken': 'late-rqtoken'},
            })
        return _FakeResponse({'errorId': 1, 'errorDescription': 'unknown'}, status_code=400)


class _CreateTaskReadyClient(_FakeAsyncClient):
    """Returns ready solution directly from createTask responses."""

    async def post(self, url, json=None, data=None, params=None):
        self.posts.append({'url': url, 'json': json})
        if 'createTask' in url:
            task_type = (json or {}).get('task', {}).get('type')
            if task_type == 'PopularPlatformSessionAction':
                return _FakeResponse({
                    'errorId': 0,
                    'status': 'ready',
                    'taskId': 'session-task-ready',
                    'solution': {'sessionId': 'session-ready', 'userAgent': 'ua-ready'},
                })
            return _FakeResponse({
                'errorId': 0,
                'status': 'ready',
                'taskId': 'captcha-task-ready',
                'cost': '0.004',
                'solution': {'token': 'token-ready', 'rqtoken': 'rq-ready'},
            })
        if 'getTaskResult' in url:
            raise AssertionError('getTaskResult should not be called for createTask-ready responses')
        return _FakeResponse({'errorId': 1, 'errorDescription': 'unknown'}, status_code=400)


def _make_db():
    engine = create_engine('sqlite:///:memory:', future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


async def _sleep_noop(*args, **kwargs):
    return None


# ------------------------------------------------------------------
# Detection
# ------------------------------------------------------------------


def test_captcha_challenge_detection():
    assert CaptchaSolverService.is_captcha_challenge({'captcha_sitekey': 'k'})
    assert CaptchaSolverService.is_captcha_challenge({'captcha_sitekey': 'k', 'captcha_rqdata': 'r'})
    assert not CaptchaSolverService.is_captcha_challenge({'captcha_rqdata': 'r'})
    assert not CaptchaSolverService.is_captcha_challenge({'captcha_sitekey': ''})
    assert not CaptchaSolverService.is_captcha_challenge({'captcha_sitekey': None})
    assert not CaptchaSolverService.is_captcha_challenge(None)


# ------------------------------------------------------------------
# Enabled / disabled
# ------------------------------------------------------------------


def test_solver_disabled_when_no_api_key(monkeypatch):
    monkeypatch.delenv('DFA_ANYSOLVER_API_KEY', raising=False)
    get_settings.cache_clear()
    assert CaptchaSolverService().is_enabled is False
    get_settings.cache_clear()


def test_solver_enabled_from_env(monkeypatch):
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'test-api-key')
    get_settings.cache_clear()
    assert CaptchaSolverService().is_enabled is True
    get_settings.cache_clear()


# ------------------------------------------------------------------
# Successful solve flow
# ------------------------------------------------------------------


def test_solver_ready_flow_and_task_type(monkeypatch):
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'test-key')
    monkeypatch.setenv('DFA_CAPTCHA_TASK_TYPE', 'PopularCaptchaEnterpriseInvisibleTokenProxyLess')
    get_settings.cache_clear()

    fake_client = _FakeAsyncClient()

    class _FakeClientFactory:
        def __call__(self, *args, **kwargs):
            fake_client.verify = kwargs.get('verify')
            return fake_client

    monkeypatch.setattr('app.services.captcha_solver.httpx.AsyncClient', _FakeClientFactory())
    monkeypatch.setattr('app.services.captcha_solver.asyncio.sleep', _sleep_noop)

    db = _make_db()
    service = CaptchaSolverService()
    result = asyncio.run(
        service.solve_discord_challenge(
            {
                'captcha_sitekey': 'site-key',
                'captcha_rqdata': 'rq-data',
                'captcha_session_id': 'discord-session-id',
                'captcha_service': 'hcaptcha',
            },
            token_id=11,
            guild_id='123',
            user_agent='ua',
            db=db,
        )
    )

    row = db.query(CaptchaChallenge).order_by(CaptchaChallenge.id.desc()).first()
    assert result['status'] == 'ready'
    assert result['captcha_key'] == 'solved-token'
    assert result['captcha_rqtoken'] == 'rq-token'
    assert result['captcha_rqdata'] == 'rq-data'
    assert row is not None
    assert row.task_id == 'captcha-task-123'
    assert row.anysolver_session_id == 'anysolver-session-123'
    assert row.solver_status == 'ready'

    # Verify AnySolver two-step flow and correct task body.
    assert fake_client.posts[0]['json']['task']['type'] == 'PopularPlatformSessionAction'
    create_call = fake_client.posts[2]
    task_json = create_call['json']['task']
    assert task_json['type'] == 'PopularCaptchaEnterpriseInvisibleTokenProxyLess'
    assert task_json['rqdata'] == 'rq-data'
    assert task_json['sessionId'] == 'anysolver-session-123'
    # Ensure Discord captcha_session_id is NOT used as AnySolver sessionId.
    assert task_json['sessionId'] != 'discord-session-id'
    # data field must NOT be present (not valid for PopularCaptcha* task types)
    assert 'data' not in task_json
    # userAgent must NOT be present (not valid for PopularCaptcha* task types)
    assert 'userAgent' not in task_json
    assert 'isInvisible' not in task_json
    assert 'pageTitle' not in task_json
    assert task_json['websiteURL'] == 'https://discord.com'
    assert task_json['websiteKey'] == 'site-key'

    get_settings.cache_clear()


def test_solver_persists_processing_then_ready(monkeypatch):
    """DB row should be finalized as 'ready' after a processing intermediate poll."""
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'test-key')
    get_settings.cache_clear()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return _ProcessingThenReadyClient(*args, **kwargs)

    monkeypatch.setattr('app.services.captcha_solver.httpx.AsyncClient', _Factory())
    monkeypatch.setattr('app.services.captcha_solver.asyncio.sleep', _sleep_noop)

    db = _make_db()
    result = asyncio.run(
        CaptchaSolverService().solve_discord_challenge(
            {'captcha_sitekey': 'sk', 'captcha_rqdata': 'rd'},
            user_agent='ua',
            db=db,
        )
    )

    assert result['status'] == 'ready'
    assert result['captcha_key'] == 'late-token'
    assert result['captcha_rqtoken'] == 'late-rqtoken'
    row = db.query(CaptchaChallenge).order_by(CaptchaChallenge.id.desc()).first()
    assert row.solver_status == 'ready'
    assert row.anysolver_session_id == 'anysolver-session-456'
    assert row.attempts == 2

    get_settings.cache_clear()


def test_solver_no_rqdata(monkeypatch):
    """Challenges without rqdata (sitekey-only) should still be solved,
    and the task body must NOT include rqdata/data keys."""
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'key')
    get_settings.cache_clear()

    captured: list[_FakeAsyncClient] = []

    class _Factory:
        def __call__(self, *args, **kwargs):
            client = _FakeAsyncClient(*args, **kwargs)
            captured.append(client)
            return client

    monkeypatch.setattr('app.services.captcha_solver.httpx.AsyncClient', _Factory())
    monkeypatch.setattr('app.services.captcha_solver.asyncio.sleep', _sleep_noop)

    result = asyncio.run(
        CaptchaSolverService().solve_discord_challenge(
            {'captcha_sitekey': 'site-key'},
            user_agent='ua',
        )
    )

    assert result['status'] == 'ready'
    assert result['captcha_key'] == 'solved-token'
    assert result['captcha_rqdata'] is None
    assert result['anysolver_session_id'] == 'anysolver-session-123'

    # Verify rqdata/data keys are absent from the task body.
    assert captured, 'Expected at least one httpx.AsyncClient to be created.'
    all_posts = [post for client in captured for post in client.posts]
    create_calls = [post for post in all_posts if 'createTask' in post['url']]
    assert create_calls[0]['json']['task']['type'] == 'PopularPlatformSessionAction'
    captcha_create_body = create_calls[1]['json']
    assert captcha_create_body['task']['sessionId'] == 'anysolver-session-123'
    assert 'rqdata' not in captcha_create_body['task']
    assert 'data' not in captcha_create_body['task']

    get_settings.cache_clear()


def test_solver_handles_create_task_ready_responses(monkeypatch):
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'key')
    get_settings.cache_clear()

    class _Factory:
        def __call__(self, *args, **kwargs):
            return _CreateTaskReadyClient(*args, **kwargs)

    monkeypatch.setattr('app.services.captcha_solver.httpx.AsyncClient', _Factory())

    result = asyncio.run(
        CaptchaSolverService().solve_discord_challenge(
            {'captcha_sitekey': 'site-key', 'captcha_rqdata': 'rq-data'},
            user_agent='ua',
        )
    )

    assert result['status'] == 'ready'
    assert result['captcha_key'] == 'token-ready'
    assert result['captcha_rqtoken'] == 'rq-ready'
    assert result['anysolver_session_id'] == 'session-ready'

    get_settings.cache_clear()


# ------------------------------------------------------------------
# SSL / TLS configuration
# ------------------------------------------------------------------


def test_ssl_verify_disabled(monkeypatch):
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'test-key')
    monkeypatch.setenv('DFA_CAPTCHA_SSL_VERIFY', 'false')
    get_settings.cache_clear()

    service = CaptchaSolverService()
    assert service.verify is False

    get_settings.cache_clear()


def test_ssl_verify_custom_ca_bundle(monkeypatch):
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'test-key')
    monkeypatch.setenv('DFA_CAPTCHA_SSL_VERIFY', 'true')
    monkeypatch.setenv('DFA_CAPTCHA_CA_BUNDLE_PATH', '/tmp/custom-ca.pem')
    get_settings.cache_clear()

    service = CaptchaSolverService()
    assert service.verify == '/tmp/custom-ca.pem'

    get_settings.cache_clear()


def test_ssl_verify_default_is_true(monkeypatch):
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'key')
    monkeypatch.delenv('DFA_CAPTCHA_SSL_VERIFY', raising=False)
    monkeypatch.delenv('DFA_CAPTCHA_CA_BUNDLE_PATH', raising=False)
    get_settings.cache_clear()

    assert CaptchaSolverService().verify is True

    get_settings.cache_clear()


# ------------------------------------------------------------------
# Error / failure paths
# ------------------------------------------------------------------


def test_solve_fails_when_not_enabled(monkeypatch):
    monkeypatch.delenv('DFA_ANYSOLVER_API_KEY', raising=False)
    get_settings.cache_clear()

    result = asyncio.run(
        CaptchaSolverService().solve_discord_challenge(
            {'captcha_sitekey': 'sk'},
            user_agent='ua',
        )
    )

    assert result['status'] == 'failed'
    assert 'DFA_ANYSOLVER_API_KEY' in result['detail']

    get_settings.cache_clear()


def test_solve_fails_on_non_challenge_payload(monkeypatch):
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'key')
    get_settings.cache_clear()

    result = asyncio.run(
        CaptchaSolverService().solve_discord_challenge(
            {'error': 'unknown'},
            user_agent='ua',
        )
    )

    assert result['status'] == 'failed'
    assert 'captcha challenge' in result['detail']

    get_settings.cache_clear()


def test_solve_fails_on_create_task_error(monkeypatch):
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'key')
    get_settings.cache_clear()

    class _ErrorClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            if 'createTask' in url:
                return _FakeResponse({'errorId': 1, 'errorDescription': 'Invalid API key'})
            return _FakeResponse({'errorId': 0, 'status': 'processing'})

    monkeypatch.setattr('app.services.captcha_solver.httpx.AsyncClient', _ErrorClient)

    result = asyncio.run(
        CaptchaSolverService().solve_discord_challenge(
            {'captcha_sitekey': 'sk'},
            user_agent='ua',
        )
    )

    assert result['status'] == 'failed'
    assert 'Invalid API key' in result['detail']

    get_settings.cache_clear()


def test_solve_writes_failed_row_to_db(monkeypatch):
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'key')
    get_settings.cache_clear()

    class _FailClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            return _FakeResponse(
                {'errorId': 1, 'errorDescription': 'quota exceeded'},
                status_code=200,
            )

    monkeypatch.setattr('app.services.captcha_solver.httpx.AsyncClient', _FailClient)

    db = _make_db()
    result = asyncio.run(
        CaptchaSolverService().solve_discord_challenge(
            {'captcha_sitekey': 'sk'},
            token_id=7,
            guild_id='guild1',
            user_agent='ua',
            db=db,
        )
    )

    assert result['status'] == 'failed'
    row = db.query(CaptchaChallenge).first()
    assert row is not None
    assert row.solver_status == 'failed'
    assert 'quota exceeded' in (row.error or '')
    assert row.token_id == 7
    assert row.guild_id == 'guild1'
    assert row.anysolver_session_id is None

    get_settings.cache_clear()


# ------------------------------------------------------------------
# proxy field in captcha task body
# ------------------------------------------------------------------


def test_proxy_included_in_captcha_task_body(monkeypatch):
    """When proxy_url is provided it must appear in the captcha task body."""
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'test-key')
    get_settings.cache_clear()

    captured: list[_FakeAsyncClient] = []

    class _Factory:
        def __call__(self, *args, **kwargs):
            client = _FakeAsyncClient(*args, **kwargs)
            captured.append(client)
            return client

    monkeypatch.setattr('app.services.captcha_solver.httpx.AsyncClient', _Factory())
    monkeypatch.setattr('app.services.captcha_solver.asyncio.sleep', _sleep_noop)

    result = asyncio.run(
        CaptchaSolverService().solve_discord_challenge(
            {'captcha_sitekey': 'site-key', 'captcha_rqdata': 'rq-data'},
            user_agent='ua',
            proxy_url='http://user:pass@proxy.example.com:8080',
        )
    )

    assert result['status'] == 'ready'

    all_posts = [post for client in captured for post in client.posts]
    create_calls = [post for post in all_posts if 'createTask' in post['url']]
    # Session task must NOT include proxy
    session_task = create_calls[0]['json']['task']
    assert session_task['type'] == 'PopularPlatformSessionAction'
    assert 'proxy' not in session_task
    # Captcha task must include proxy
    captcha_task = create_calls[1]['json']['task']
    assert captcha_task['proxy'] == 'http://user:pass@proxy.example.com:8080'

    get_settings.cache_clear()


def test_proxy_absent_when_not_provided(monkeypatch):
    """When no proxy_url is given the captcha task body must not include a proxy field."""
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'test-key')
    get_settings.cache_clear()

    captured: list[_FakeAsyncClient] = []

    class _Factory:
        def __call__(self, *args, **kwargs):
            client = _FakeAsyncClient(*args, **kwargs)
            captured.append(client)
            return client

    monkeypatch.setattr('app.services.captcha_solver.httpx.AsyncClient', _Factory())
    monkeypatch.setattr('app.services.captcha_solver.asyncio.sleep', _sleep_noop)

    result = asyncio.run(
        CaptchaSolverService().solve_discord_challenge(
            {'captcha_sitekey': 'site-key'},
            user_agent='ua',
        )
    )

    assert result['status'] == 'ready'

    all_posts = [post for client in captured for post in client.posts]
    create_calls = [post for post in all_posts if 'createTask' in post['url']]
    captcha_task = create_calls[1]['json']['task']
    assert 'proxy' not in captcha_task

    get_settings.cache_clear()


# ------------------------------------------------------------------
# provider field in createTask request body
# ------------------------------------------------------------------


def test_provider_included_in_create_task_body(monkeypatch):
    """When DFA_CAPTCHA_PROVIDER is set it must appear in every createTask body."""
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'test-key')
    monkeypatch.setenv('DFA_CAPTCHA_PROVIDER', 'EZCaptcha')
    get_settings.cache_clear()

    captured: list[_FakeAsyncClient] = []

    class _Factory:
        def __call__(self, *args, **kwargs):
            client = _FakeAsyncClient(*args, **kwargs)
            captured.append(client)
            return client

    monkeypatch.setattr('app.services.captcha_solver.httpx.AsyncClient', _Factory())
    monkeypatch.setattr('app.services.captcha_solver.asyncio.sleep', _sleep_noop)

    result = asyncio.run(
        CaptchaSolverService().solve_discord_challenge(
            {'captcha_sitekey': 'site-key'},
            user_agent='ua',
        )
    )

    assert result['status'] == 'ready'

    all_posts = [post for client in captured for post in client.posts]
    create_calls = [post for post in all_posts if 'createTask' in post['url']]
    for call in create_calls:
        assert call['json']['provider'] == 'EZCaptcha', (
            f"provider missing from createTask body: {call['json']}"
        )

    get_settings.cache_clear()


def test_provider_absent_when_not_configured(monkeypatch):
    """When DFA_CAPTCHA_PROVIDER is not set the provider key must be absent."""
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'test-key')
    monkeypatch.delenv('DFA_CAPTCHA_PROVIDER', raising=False)
    get_settings.cache_clear()

    captured: list[_FakeAsyncClient] = []

    class _Factory:
        def __call__(self, *args, **kwargs):
            client = _FakeAsyncClient(*args, **kwargs)
            captured.append(client)
            return client

    monkeypatch.setattr('app.services.captcha_solver.httpx.AsyncClient', _Factory())
    monkeypatch.setattr('app.services.captcha_solver.asyncio.sleep', _sleep_noop)

    result = asyncio.run(
        CaptchaSolverService().solve_discord_challenge(
            {'captcha_sitekey': 'site-key'},
            user_agent='ua',
        )
    )

    assert result['status'] == 'ready'

    all_posts = [post for client in captured for post in client.posts]
    create_calls = [post for post in all_posts if 'createTask' in post['url']]
    for call in create_calls:
        assert 'provider' not in call['json'], (
            f"provider unexpectedly present in createTask body: {call['json']}"
        )

    get_settings.cache_clear()
