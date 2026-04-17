import pytest

from app.core.config import get_settings


def test_settings_rejects_invalid_runtype(monkeypatch):
    monkeypatch.setenv('DFA_RUNTYPE', 'INVALID')
    monkeypatch.delenv('DFA_DISCORD_BOT_TOKEN', raising=False)
    get_settings.cache_clear()
    with pytest.raises(Exception):
        get_settings()
    get_settings.cache_clear()


def test_settings_requires_bot_token_in_bott(monkeypatch):
    monkeypatch.setenv('DFA_RUNTYPE', 'BOTT')
    monkeypatch.setenv('DFA_DISCORD_BOT_TOKEN', '')
    get_settings.cache_clear()
    with pytest.raises(Exception):
        get_settings()
    get_settings.cache_clear()


def test_settings_accepts_bott_with_token(monkeypatch):
    monkeypatch.setenv('DFA_RUNTYPE', 'BOTT')
    monkeypatch.setenv('DFA_DISCORD_BOT_TOKEN', 'bot-token-value')
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.runtype == 'BOTT'
    assert settings.discord_bot_token == 'bot-token-value'
    get_settings.cache_clear()
