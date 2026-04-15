import pytest
from unittest.mock import AsyncMock, patch

from app.services.ai_chat import AIChatService


@pytest.mark.asyncio
async def test_ai_chat_no_api_key():
    with patch('app.services.ai_chat.get_settings') as mock_settings:
        mock_settings.return_value.openrouter_api_key = ''
        mock_settings.return_value.openrouter_model = 'x-ai/grok-4.1-fast'
        service = AIChatService()
        result = await service.chat('Hello')
        assert '[AI service unavailable' in result['response']
        assert result['model'] == 'x-ai/grok-4.1-fast'
