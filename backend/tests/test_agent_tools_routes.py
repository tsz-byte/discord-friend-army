from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.db.session import Base, engine
from app.main import app

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def test_tools_help_lists_agent_tools():
    response = client.get('/api/v1/agent/tools/help')
    assert response.status_code == 200
    tools = response.json()['tools']
    assert any(item['name'] == 'channel__send_embed' for item in tools)


def test_campaign_invite_boost_uses_request_params(monkeypatch):
    captured = {}

    async def _fake_send_embed(**kwargs):
        captured.update(kwargs)
        return {'status': 'sent'}

    async def _fake_resolve(channel_id_or_name, guild_id, db):
        return '1234567890'

    monkeypatch.setattr('app.api.routes_agent_tools._select_token', lambda db, token_id=None: SimpleNamespace(token_value='tok', proxy_host=None, proxy_port=None, proxy_username=None, proxy_password=None))
    monkeypatch.setattr('app.api.routes_agent_tools._resolve_channel_id', _fake_resolve)
    monkeypatch.setattr('app.api.routes_agent_tools.discord_client.send_embed', _fake_send_embed)

    response = client.post(
        '/api/v1/agent/campaign/invite-boost',
        json={
            'channel_id': '#partners',
            'guild_id': '1',
            'invite_link': 'https://discord.gg/test',
            'title': 'Partner Campaign',
            'description': 'Custom body',
            'content': 'Ping text',
            'mention_everyone': True,
            'fields': [{'name': 'Reward', 'value': 'Nitro'}],
        },
    )
    assert response.status_code == 200
    assert response.json()['status'] == 'sent'
    assert captured['title'] == 'Partner Campaign'
    assert captured['description'] == 'Custom body'
    assert captured['content'] == 'Ping text'
    assert captured['mention_everyone'] is True
    assert any(item['name'] == 'Invite Link' and item['value'] == 'https://discord.gg/test' for item in captured['fields'])


def test_ai_agent_endpoint_uses_agent_loop(monkeypatch):
    async def _fake_agent_chat(**kwargs):
        return {
            'response': 'done',
            'steps': [{'tool': 'channel__send_embed'}],
            'tool_calls_made': 1,
            'model': 'test-model',
            'usage': {},
        }

    monkeypatch.setattr('app.api.routes.ai_service.agent_chat', _fake_agent_chat)
    response = client.post('/api/v1/ai/agent', json={'message': 'post this'})
    assert response.status_code == 200
    data = response.json()
    assert data['response'] == 'done'
    assert data['tool_calls_made'] == 1
