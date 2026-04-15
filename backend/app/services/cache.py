import json
from typing import Any

import redis
from redis.exceptions import RedisError

from app.core.config import get_settings


class CacheService:
    def __init__(self) -> None:
        settings = get_settings()
        self.client = redis.from_url(settings.redis_url, decode_responses=True)
        self.ttl = settings.analytics_cache_ttl_seconds
        self._memory: dict[str, Any] = {}
        self._redis_enabled = True
        try:
            self.client.ping()
        except RedisError:
            self._redis_enabled = False

    def get_json(self, key: str) -> Any:
        if not self._redis_enabled:
            return self._memory.get(key)
        value = self.client.get(key)
        if not value:
            return None
        return json.loads(value)

    def set_json(self, key: str, value: Any) -> None:
        if not self._redis_enabled:
            self._memory[key] = value
            return
        self.client.setex(key, self.ttl, json.dumps(value))

    def delete(self, key: str) -> None:
        if not self._redis_enabled:
            self._memory.pop(key, None)
            return
        self.client.delete(key)
