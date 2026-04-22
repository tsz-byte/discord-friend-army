import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.services.token_manager import TokenManagerService


def _make_db():
    engine = create_engine('sqlite:///:memory:', future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_normalize_rejects_bot_prefix():
    manager = TokenManagerService()
    with pytest.raises(ValueError, match='must not include "Bot "'):
        manager.normalize_token_value('Bot abcdefghijklmnopqrstuvwxyz')


@pytest.mark.asyncio
async def test_health_check_sets_username(monkeypatch):
    db = _make_db()
    manager = TokenManagerService()
    token = manager.upsert_token(
        db=db,
        label='token-1',
        raw_token_value='MTE5NjY2MDkwNjkwNjYyODE2OA.GssFyI.jZ9kiJ1uBwtKjn6VYM3GAeTiBPsA8R_kq92XhE',
        rotation_priority=10,
    )

    class _FakeResponse:
        status_code = 200
        text = '{"username":"demo-user"}'

        @staticmethod
        def json():
            return {'username': 'demo-user'}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr('app.services.token_manager.httpx.AsyncClient', _FakeAsyncClient)
    checked = await manager.health_check(db, token)
    assert checked.health_status == 'healthy'
    assert checked.source_identity == 'demo-user'


@pytest.mark.asyncio
async def test_health_check_reactivates_token_on_success(monkeypatch):
    db = _make_db()
    manager = TokenManagerService()
    token = manager.upsert_token(
        db=db,
        label='token-2',
        raw_token_value='MTE5NjY2MDkwNjkwNjYyODE2OA.GssFyI.jZ9kiJ1uBwtKjn6VYM3GAeTiBPsA8R_kq92XhE',
        rotation_priority=20,
    )
    token.is_active = False
    db.commit()

    class _FakeResponse:
        status_code = 200
        text = '{"username":"reactivated-user"}'

        @staticmethod
        def json():
            return {'username': 'reactivated-user'}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            return _FakeResponse()

    monkeypatch.setattr('app.services.token_manager.httpx.AsyncClient', _FakeAsyncClient)
    checked = await manager.health_check(db, token)
    assert checked.health_status == 'healthy'
    assert checked.is_active is True


@pytest.mark.asyncio
async def test_health_check_does_not_deactivate_on_401(monkeypatch):
    """A 401 from the health-check endpoint must mark the token invalid but
    must NOT force is_active=False — that avoids false positives from transient
    proxy / Discord API errors."""
    db = _make_db()
    manager = TokenManagerService()
    token = manager.upsert_token(
        db=db,
        label='token-3',
        raw_token_value='MTE5NjY2MDkwNjkwNjYyODE2OA.GssFyI.jZ9kiJ1uBwtKjn6VYM3GAeTiBPsA8R_kq92XhE',
        rotation_priority=30,
    )
    assert token.is_active is True

    class _Fake401Response:
        status_code = 401
        text = '{"message": "401: Unauthorized"}'
        headers = {}

        @staticmethod
        def json():
            return {'message': '401: Unauthorized'}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *args, **kwargs):
            return _Fake401Response()

    monkeypatch.setattr('app.services.token_manager.httpx.AsyncClient', _FakeAsyncClient)
    checked = await manager.health_check(db, token)
    assert checked.health_status == 'invalid'
    # Token must stay active so a subsequent successful check can re-enable it.
    assert checked.is_active is True


def test_should_mark_invalid_from_result_only_for_auth_failures():
    manager = TokenManagerService()
    assert manager.should_mark_invalid_from_result({'code': 401}) == (True, True)
    assert manager.should_mark_invalid_from_result({'code': 403, 'error_code': 40001}) == (True, False)
    assert manager.should_mark_invalid_from_result({'code': 403, 'error_code': 50001, 'detail': 'Missing Access'}) == (False, False)
