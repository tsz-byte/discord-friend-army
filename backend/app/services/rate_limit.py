import time
from collections import deque

from redis.exceptions import RedisError

from app.services.cache import CacheService


class DiscordRateLimiter:
    def __init__(self, limit_per_minute: int) -> None:
        self.limit_per_minute = limit_per_minute
        self._fallback_buckets: dict[str, deque[float]] = {}
        self.cache = CacheService()

    def check(self, scope: str) -> tuple[bool, int]:
        key = f'ratelimit:{scope}'
        try:
            count = self.cache.client.incr(key)
            if count == 1:
                self.cache.client.expire(key, 60)
            if count > self.limit_per_minute:
                ttl = self.cache.client.ttl(key)
                return False, max(ttl, 1)
            return True, 0
        except RedisError:
            return self._check_fallback(scope)

    def _check_fallback(self, scope: str) -> tuple[bool, int]:
        now = time.time()
        bucket = self._fallback_buckets.setdefault(scope, deque())
        while bucket and now - bucket[0] >= 60:
            bucket.popleft()
        if len(bucket) >= self.limit_per_minute:
            retry_after = int(max(1, 60 - (now - bucket[0])))
            return False, retry_after
        bucket.append(now)
        return True, 0
