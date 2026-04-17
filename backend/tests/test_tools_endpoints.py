from fastapi.testclient import TestClient

from app.db.session import Base, engine
from app.main import app
from app.services.discord_client import DiscordClient

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def test_request_has_correlation_header():
    response = client.get('/health')
    assert response.status_code == 200
    assert response.headers.get('X-Correlation-ID')


def test_server_joiner_rejects_invalid_invite():
    response = client.post(
        '/api/v1/tools/server-joiner/join',
        json={
            'guild_id': '123',
            'invite_code': '??',
            'token_ids': [],
            'auto_onboarding': True,
            'use_proxies': True,
        },
    )
    assert response.status_code == 400


def test_clan_tag_bulk_generate():
    response = client.post(
        '/api/v1/tools/clan-tag/bulk-generate',
        json={'template': '[{num}] {tag}', 'base_tag': 'Gaming', 'start_number': 3},
    )
    assert response.status_code == 200
    data = response.json()
    assert data['generated'][0] == '[3] Gaming'


def test_invite_code_extraction_and_retry_after_parser():
    assert DiscordClient.extract_invite_code('https://discord.gg/abc123') == 'abc123'
    assert DiscordClient.extract_invite_code('discord.com/invite/xyz987') == 'xyz987'
