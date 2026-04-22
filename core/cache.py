"""
core/cache.py — Redis cache wrapper with TTL helpers.
Falls back to a simple in-process dict when Redis is unavailable.
"""

import asyncio
import fnmatch
import json
import logging
import time
from typing import Any, Callable, Coroutine, Optional

from core.settings import get_settings

logger = logging.getLogger(__name__)

SCHEMA_CACHE_KEY = "maximo:openapi_schema"
SCHEMA_CACHE_TTL = 86400  # 24 hours

# How long (seconds) to wait before retrying a failed Redis connection
_REDIS_RETRY_INTERVAL = 30


class _InMemoryFallback:
    """Simple dict-based in-memory cache with TTL enforcement."""

    def __init__(self):
        # Stores (value, expires_at) pairs; expires_at=0 means no expiry
        self._store: dict[str, tuple[str, float]] = {}

    async def get(self, key: str) -> Optional[str]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at and time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    async def set(self, key: str, value: str, ex: int = 300) -> None:
        expires_at = time.monotonic() + ex if ex else 0.0
        self._store[key] = (value, expires_at)

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def keys(self, pattern: str) -> list[str]:
        now = time.monotonic()
        return [
            k for k, (_, exp) in list(self._store.items())
            if fnmatch.fnmatch(k, pattern) and (not exp or now <= exp)
        ]

    async def ping(self) -> bool:
        return True


class CacheClient:
    """
    Async Redis cache wrapper.
    All values are JSON-serialised strings.
    """

    def __init__(self):
        self.settings = get_settings()
        self._redis: Any = None
        self._fallback = _InMemoryFallback()
        self._use_fallback = False
        self._next_redis_retry: float = 0.0  # monotonic time; 0 = retry immediately

    async def _get_redis(self) -> Any:
        # Already have a working Redis client
        if self._redis is not None:
            return self._redis

        # Cache explicitly disabled
        if not self.settings.CACHE_ENABLED:
            self._use_fallback = True
            return self._fallback

        # Respect retry back-off to avoid hammering unavailable Redis
        if self._use_fallback and time.monotonic() < self._next_redis_retry:
            return self._fallback

        try:
            import redis.asyncio as aioredis  # type: ignore
            redis_kwargs: dict = {
                "encoding": "utf-8",
                "decode_responses": True,
                "socket_connect_timeout": 2,
                "socket_timeout": 5,
            }
            # Inject password if configured (avoids embedding credentials in the URL)
            if self.settings.REDIS_PASSWORD:
                redis_kwargs["password"] = self.settings.REDIS_PASSWORD
            client = aioredis.from_url(self.settings.REDIS_URL, **redis_kwargs)
            await client.ping()
            self._redis = client
            self._use_fallback = False
            logger.info("Redis connected at %s", self.settings.REDIS_URL)
            return self._redis
        except Exception as exc:
            logger.warning(
                "Redis unavailable (%s); using in-memory cache. Retrying in %ds.",
                exc, _REDIS_RETRY_INTERVAL,
            )
            self._use_fallback = True
            self._next_redis_retry = time.monotonic() + _REDIS_RETRY_INTERVAL
            return self._fallback

    async def get(self, key: str) -> Optional[Any]:
        """Retrieve a cached value; returns None on miss."""
        r = await self._get_redis()
        try:
            raw = await r.get(key)
        except Exception as exc:
            logger.warning("Cache GET error for '%s': %s", key, exc)
            self._redis = None
            self._next_redis_retry = time.monotonic() + _REDIS_RETRY_INTERVAL
            return None
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    async def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        """Store a value with optional TTL (seconds). Defaults to CACHE_TTL_SECONDS."""
        r = await self._get_redis()
        ttl = ttl if ttl is not None else self.settings.CACHE_TTL_SECONDS
        serialised = json.dumps(value, default=str)
        try:
            await r.set(key, serialised, ex=ttl)
        except Exception as exc:
            logger.warning("Cache SET error for '%s': %s", key, exc)
            self._redis = None
            self._next_redis_retry = time.monotonic() + _REDIS_RETRY_INTERVAL

    async def delete(self, key: str) -> None:
        """Remove a single cache entry."""
        r = await self._get_redis()
        try:
            await r.delete(key)
        except Exception as exc:
            logger.warning("Cache DELETE error for '%s': %s", key, exc)
            self._redis = None
            self._next_redis_retry = time.monotonic() + _REDIS_RETRY_INTERVAL

    async def invalidate(self, pattern: str) -> int:
        """
        Delete all keys matching a glob pattern.
        Uses SCAN + pipeline on Redis (non-blocking); simple loop on fallback.
        Returns the number of deleted keys.
        """
        r = await self._get_redis()

        if self._use_fallback:
            # In-memory fallback: keys() already handles TTL filtering
            keys = await r.keys(pattern)
            for key in keys:
                await r.delete(key)
            if keys:
                logger.info("Cache invalidated %d keys matching '%s'", len(keys), pattern)
            return len(keys)

        # Redis: use SCAN to avoid blocking, pipeline deletes for efficiency
        deleted = 0
        cursor = 0
        pipeline = r.pipeline()
        try:
            while True:
                cursor, keys = await r.scan(cursor, match=pattern, count=100)
                for key in keys:
                    pipeline.delete(key)
                    deleted += 1
                if cursor == 0:
                    break
            if deleted:
                await pipeline.execute()
                logger.info("Cache invalidated %d keys matching '%s'", deleted, pattern)
        except Exception as exc:
            logger.warning("Cache invalidate failed for pattern '%s': %s", pattern, exc)
            # Reset Redis client so next request triggers reconnect
            self._redis = None
            self._next_redis_retry = time.monotonic() + _REDIS_RETRY_INTERVAL
        return deleted

    async def get_or_fetch(
        self,
        key: str,
        fetch_fn: Callable[[], Coroutine[Any, Any, Any]],
        ttl: Optional[int] = None,
    ) -> tuple[Any, bool]:
        """
        Return (value, from_cache) — fetching and caching on miss.

        Args:
            key:      Cache key
            fetch_fn: Async callable that returns the value when cache misses
            ttl:      TTL in seconds; defaults to CACHE_TTL_SECONDS

        Returns:
            (data, cached) — cached=True when the value came from Redis
        """
        cached = await self.get(key)
        if cached is not None:
            logger.debug("Cache HIT: %s", key)
            return cached, True

        logger.debug("Cache MISS: %s", key)
        value = await fetch_fn()
        await self.set(key, value, ttl=ttl)
        return value, False

    async def get_status(self) -> dict[str, Any]:
        """Return a public cache health snapshot without exposing internals."""
        backend = "in-memory" if self._use_fallback or not self.settings.CACHE_ENABLED else "redis"
        healthy = False
        try:
            client = await self._get_redis()
            ping = await client.ping()
            healthy = bool(ping)
            backend = "in-memory" if self._use_fallback else "redis"
        except Exception:
            healthy = False
            backend = "in-memory"
        return {
            "backend": backend,
            "healthy": healthy,
            "cache_enabled": self.settings.CACHE_ENABLED,
        }


# ── Module-level singleton ────────────────────────────────────────────────────

_cache_instance: Optional[CacheClient] = None


def get_cache() -> CacheClient:
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = CacheClient()
    return _cache_instance
