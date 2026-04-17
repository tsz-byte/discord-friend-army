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
            raise httpx.HTTPStatusError('http error', request=httpx.Request('POST', 'https://example.com'), response=httpx.Response(self.status_code))


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self.verify = kwargs.get('verify')
        self.timeout = kwargs.get('timeout')
        self.posts = []
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, data=None, params=None):
        self.posts.append({'url': url, 'json': json, 'data': data, 'params': params})
        if 'api.anysolver.com/createTask' in url:
            return _FakeResponse({'errorId': 0, 'taskId': 'task-123'})
        if 'api.anysolver.com/getTaskResult' in url:
            return _FakeResponse({'errorId': 0, 'status': 'ready', 'cost': '0.0025', 'solution': {'gRecaptchaResponse': 'solved-token', 'rqtoken': 'rq-token'}})
        if 'api.2captcha.com/createTask' in url:
            return _FakeResponse({'errorId': 0, 'taskId': 'task-2captcha'})
        if 'api.2captcha.com/getTaskResult' in url:
            return _FakeResponse({'errorId': 0, 'status': 'ready', 'solution': {'token': 'solved-by-2captcha'}})
        return _FakeResponse({'errorId': 1, 'errorDescription': 'unknown endpoint'}, status_code=400)

    async def get(self, url, params=None):
        self.gets.append({'url': url, 'params': params})
        return _FakeResponse({'captcha': '0'})


class _FallbackAsyncClient(_FakeAsyncClient):
    async def post(self, url, json=None, data=None, params=None):
        self.posts.append({'url': url, 'json': json, 'data': data, 'params': params})
        if 'api.anysolver.com/createTask' in url:
            raise httpx.ConnectError('ssl failed', request=httpx.Request('POST', url))
        if 'api.2captcha.com/createTask' in url:
            return _FakeResponse({'errorId': 0, 'taskId': 'task-2captcha'})
        if 'api.2captcha.com/getTaskResult' in url:
            return _FakeResponse({'errorId': 0, 'status': 'ready', 'solution': {'token': 'fallback-token'}})
        return _FakeResponse({'errorId': 1, 'errorDescription': 'unknown endpoint'}, status_code=400)


def _make_db():
    engine = create_engine('sqlite:///:memory:', future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


async def _sleep_noop(*args, **kwargs):
    return None


def test_captcha_challenge_detection():
    assert CaptchaSolverService.is_captcha_challenge({'captcha_sitekey': 'k'})
    assert CaptchaSolverService.is_captcha_challenge({'captcha_sitekey': 'k', 'captcha_rqdata': 'r'})
    assert not CaptchaSolverService.is_captcha_challenge({'captcha_rqdata': 'r'})
    assert not CaptchaSolverService.is_captcha_challenge({'captcha_sitekey': ''})
    assert not CaptchaSolverService.is_captcha_challenge({'captcha_sitekey': None})
    assert not CaptchaSolverService.is_captcha_challenge(None)


def test_solver_enabled_from_env(monkeypatch):
    monkeypatch.delenv('DFA_CAPTCHA_API_KEY', raising=False)
    monkeypatch.delenv('DFA_ANYSOLVER_API_KEY', raising=False)
    get_settings.cache_clear()
    assert CaptchaSolverService().is_enabled is False

    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'legacy-anysolver-key')
    get_settings.cache_clear()
    assert CaptchaSolverService().is_enabled is True

    get_settings.cache_clear()


def test_solver_ready_flow_and_task_type(monkeypatch):
    monkeypatch.setenv('DFA_CAPTCHA_SERVICE', 'anysolver')
    monkeypatch.setenv('DFA_CAPTCHA_API_KEY', 'test-key')
    monkeypatch.setenv('DFA_CAPTCHA_TASK_TYPE', 'HCaptchaTaskProxyless')
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
    assert row.task_id == 'task-123'
    assert row.solver_status == 'ready'

    create_call = fake_client.posts[0]
    assert create_call['json']['task']['type'] == 'HCaptchaTaskProxyless'
    assert create_call['json']['task']['rqdata'] == 'rq-data'
    assert create_call['json']['task']['data'] == 'rq-data'

    get_settings.cache_clear()


def test_fallback_to_next_service(monkeypatch):
    monkeypatch.setenv('DFA_CAPTCHA_SERVICE', 'anysolver')
    monkeypatch.setenv('DFA_CAPTCHA_FALLBACK_SERVICES', '2captcha')
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'anysolver-key')
    monkeypatch.setenv('DFA_CAPTCHA_2CAPTCHA_API_KEY', '2captcha-key')
    get_settings.cache_clear()

    monkeypatch.setattr('app.services.captcha_solver.httpx.AsyncClient', _FallbackAsyncClient)
    monkeypatch.setattr('app.services.captcha_solver.asyncio.sleep', _sleep_noop)

    result = asyncio.run(
        CaptchaSolverService().solve_discord_challenge(
            {
                'captcha_sitekey': 'site-key',
                'captcha_rqdata': 'rq-data',
                'captcha_service': 'hcaptcha',
            },
            user_agent='ua',
        )
    )

    assert result['status'] == 'ready'
    assert result['service'] == '2captcha'
    assert result['captcha_key'] == 'fallback-token'

    get_settings.cache_clear()


def test_ssl_verify_configuration(monkeypatch):
    monkeypatch.setenv('DFA_CAPTCHA_SERVICE', 'anysolver')
    monkeypatch.setenv('DFA_CAPTCHA_API_KEY', 'test-key')
    monkeypatch.setenv('DFA_CAPTCHA_SSL_VERIFY', 'false')
    get_settings.cache_clear()

    service = CaptchaSolverService()
    assert service._services['anysolver'].verify is False

    monkeypatch.setenv('DFA_CAPTCHA_SSL_VERIFY', 'true')
    monkeypatch.setenv('DFA_CAPTCHA_CA_BUNDLE_PATH', '/tmp/custom-ca.pem')
    get_settings.cache_clear()

    service_with_ca = CaptchaSolverService()
    assert service_with_ca._services['anysolver'].verify == '/tmp/custom-ca.pem'

    get_settings.cache_clear()
