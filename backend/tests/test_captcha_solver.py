from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.db.session import Base
from app.models.research import CaptchaChallenge
from app.services.captcha_solver import CaptchaSolverService


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('http error')


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self.responses = [
            _FakeResponse({'errorId': 0, 'taskId': 'task-123'}),
            _FakeResponse({'errorId': 0, 'status': 'processing'}),
            _FakeResponse({'errorId': 0, 'status': 'ready', 'cost': '0.0025', 'solution': {'token': 'solved-token', 'rqtoken': 'rq-token'}}),
        ]

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return self.responses.pop(0)


def _make_db():
    engine = create_engine('sqlite:///:memory:', future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_captcha_challenge_detection():
    assert CaptchaSolverService.is_captcha_challenge({'captcha_sitekey': 'k', 'captcha_rqdata': 'r'})
    assert not CaptchaSolverService.is_captcha_challenge({'captcha_sitekey': 'k'})


async def _sleep_noop(*args, **kwargs):
    return None


def test_solver_ready_flow(monkeypatch):
    monkeypatch.setenv('DFA_ANYSOLVER_API_KEY', 'test-key')
    get_settings.cache_clear()

    monkeypatch.setattr('app.services.captcha_solver.httpx.AsyncClient', _FakeAsyncClient)
    monkeypatch.setattr('app.services.captcha_solver.asyncio.sleep', _sleep_noop)

    db = _make_db()
    service = CaptchaSolverService()
    result = __import__('asyncio').run(
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
    assert row is not None
    assert row.task_id == 'task-123'
    assert row.solver_status == 'ready'

    get_settings.cache_clear()
