from __future__ import annotations

import json
import httpx

from app.core.config import get_settings


class AIChatService:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def chat(
        self,
        message: str,
        conversation_history: list[dict] | None = None,
        system_prompt: str = 'You are a helpful Discord community assistant.',
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        api_key = self.settings.openrouter_api_key
        if not api_key:
            return {
                'response': '[AI service unavailable - no API key configured]',
                'model': self.settings.openrouter_model,
                'usage': {},
            }

        messages = [{'role': 'system', 'content': system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({'role': 'user', 'content': message[:4000]})

        payload = {
            'model': self.settings.openrouter_model,
            'messages': messages,
            'max_tokens': max_tokens or self.settings.openrouter_max_tokens,
            'temperature': temperature if temperature is not None else self.settings.openrouter_temperature,
        }
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
        timeout = self.settings.openrouter_response_timeout

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    'https://openrouter.ai/api/v1/chat/completions',
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                text = data['choices'][0]['message']['content']
                usage = data.get('usage', {})
                return {
                    'response': text,
                    'model': self.settings.openrouter_model,
                    'usage': usage,
                }
        except httpx.HTTPStatusError as exc:
            return {
                'response': f'[AI error: HTTP {exc.response.status_code}]',
                'model': self.settings.openrouter_model,
                'usage': {},
            }
        except httpx.HTTPError as exc:
            return {
                'response': f'[AI error: {type(exc).__name__}]',
                'model': self.settings.openrouter_model,
                'usage': {},
            }
