import os
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.session import Base
from app.services.file_loader import FileLoaderService


def _make_db():
    engine = create_engine('sqlite:///:memory:', future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)()


def test_load_tokens_from_file():
    db = _make_db()
    loader = FileLoaderService()
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write('# comment line\n')
        f.write('MTQ4MzU0NTA5MjU4ODMxMDY2OQ.GSITFd.bVNznSTbUb_sskxAVZMZnIeAfqhGuSI-ld8x_8\n')
        f.write('\n')
        f.write('MTE5NjY2MDkwNjkwNjYyODE2OA.GssFyI.jZ9kiJ1uBwtKjn6VYM3GAeTiBPsA8R_kq92XhE\n')
        f.name
    try:
        loaded, errors = loader.load_tokens_file(db, f.name)
        assert loaded == 2
        assert errors == []
    finally:
        os.unlink(f.name)


def test_load_tokens_missing_file():
    db = _make_db()
    loader = FileLoaderService()
    loaded, errors = loader.load_tokens_file(db, '/nonexistent/t.txt')
    assert loaded == 0
    assert len(errors) == 1


def test_load_proxies_from_file():
    db = _make_db()
    loader = FileLoaderService()
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write('# proxies\n')
        f.write('pr-eu.proxies.fo:13337:user-session-abc:password123\n')
        f.write('proxy-server.com:8080:username:password\n')
    try:
        loaded, errors = loader.load_proxies_file(db, f.name)
        assert loaded == 2
        assert errors == []
    finally:
        os.unlink(f.name)


def test_load_proxies_missing_file():
    db = _make_db()
    loader = FileLoaderService()
    loaded, errors = loader.load_proxies_file(db, '/nonexistent/p.txt')
    assert loaded == 0
    assert len(errors) == 1


def test_load_api_config():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
        f.write('# API Config\n')
        f.write('OPENROUTER_API_KEY=sk-or-v1-test\n')
        f.write('AI_MODEL=x-ai/grok-4.1-fast\n')
    try:
        config = FileLoaderService.load_api_config(f.name)
        assert config['OPENROUTER_API_KEY'] == 'sk-or-v1-test'
        assert config['AI_MODEL'] == 'x-ai/grok-4.1-fast'
    finally:
        os.unlink(f.name)


def test_load_api_config_missing():
    config = FileLoaderService.load_api_config('/nonexistent/api_key.conf')
    assert config == {}
