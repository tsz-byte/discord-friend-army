import re

import httpx

from app.core.config import get_settings


class OpenRouterNLPService:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def analyze(self, content: str) -> dict:
        if not self.settings.openrouter_api_key:
            return self._fallback_analysis(content)

        payload = {
            'model': self.settings.openrouter_model,
            'messages': [
                {
                    'role': 'system',
                    'content': 'Return compact JSON with keys sentiment, score, topics for discord research analytics.',
                },
                {'role': 'user', 'content': content[:1200]},
            ],
            'response_format': {'type': 'json_object'},
        }
        headers = {
            'Authorization': f'Bearer {self.settings.openrouter_api_key}',
            'Content-Type': 'application/json',
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post('https://openrouter.ai/api/v1/chat/completions', json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            text = data['choices'][0]['message']['content']
            try:
                import json

                parsed = json.loads(text)
                return {
                    'sentiment': parsed.get('sentiment', 'neutral'),
                    'score': float(parsed.get('score', 0.0)),
                    'topics': parsed.get('topics', []),
                }
            except Exception:
                return self._fallback_analysis(content)

    @staticmethod
    def _fallback_analysis(content: str) -> dict:
        lower = content.lower()
        positive = sum(word in lower for word in ['thanks', 'great', 'awesome', 'love'])
        negative = sum(word in lower for word in ['hate', 'bad', 'angry', 'sad'])
        score = positive - negative
        sentiment = 'neutral' if score == 0 else ('positive' if score > 0 else 'negative')

        words = [w for w in re.findall(r'[a-zA-Z]{4,}', lower) if w not in {'that', 'this', 'with', 'have'}]
        topics = sorted(set(words), key=lambda w: (-words.count(w), w))[:5]
        return {'sentiment': sentiment, 'score': float(score), 'topics': topics}
