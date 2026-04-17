from fastapi.testclient import TestClient

from app.db.session import Base, engine
from app.main import app

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def test_dashboard_stats():
    response = client.get('/api/v1/dashboard/stats')
    assert response.status_code == 200
    data = response.json()
    assert 'active_accounts' in data
    assert 'total_proxies' in data
    assert 'uptime_seconds' in data


def test_proxy_health():
    response = client.get('/api/v1/proxies/health')
    assert response.status_code == 200
    data = response.json()
    assert 'total' in data
    assert 'proxies' in data


def test_settings_update():
    response = client.patch(
        '/api/v1/settings/update',
        json={'key': 'test_key', 'value': 'test_value'},
    )
    assert response.status_code == 200
    assert response.json()['status'] == 'saved'


def test_load_config():
    response = client.post('/api/v1/config/load-file')
    assert response.status_code == 200


def test_ai_chat():
    response = client.post(
        '/api/v1/ai/chat',
        json={'message': 'Hello, how are you?'},
    )
    assert response.status_code == 200
    data = response.json()
    assert 'response' in data
    assert 'model' in data


def test_get_runtype_setting():
    response = client.get('/api/v1/settings/runtype')
    assert response.status_code == 200
    data = response.json()
    assert data['runtype'] in {'USERT', 'BOTT'}
    assert isinstance(data['bot_token_configured'], bool)


def test_patch_runtype_requires_bot_token_for_bott():
    client.patch('/api/v1/settings/runtype', json={'runtype': 'USERT', 'discord_bot_token': ''})
    response = client.patch('/api/v1/settings/runtype', json={'runtype': 'BOTT', 'discord_bot_token': ''})
    assert response.status_code == 400


def test_patch_runtype_accepts_bott_with_token():
    response = client.patch('/api/v1/settings/runtype', json={'runtype': 'BOTT', 'discord_bot_token': 'bot-token-abc'})
    assert response.status_code == 200
    data = response.json()
    assert data['runtype'] == 'BOTT'
    assert data['bot_token_configured'] is True
    # restore default mode for other tests
    restore = client.patch('/api/v1/settings/runtype', json={'runtype': 'USERT'})
    assert restore.status_code == 200
