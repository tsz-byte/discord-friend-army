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
