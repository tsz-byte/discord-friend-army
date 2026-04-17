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
