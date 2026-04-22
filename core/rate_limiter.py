"""
core/rate_limiter.py — Simple in-memory per-user rate limiter.

Uses a sliding-window counter keyed by user_id.
RATE_LIMIT_PER_MINUTE from settings controls the maximum calls per minute.
Falls back gracefully (allow) on any internal error so it never blocks
legitimate requests due to a bug in the limiter itself.

Usage (applied inside core/rbac.py's require_role decorator):
    from core.rate_limiter import get_rate_limiter
    allowed = await get_rate_limiter().check(user_id)
    if not allowed:
        return {"success": False, "error": "Rate limit exceeded", "error_code": "RATE_LIMITED"}
"""

import asyncio
import logging
import time
from collections import deque
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Async-safe sliding-window rate limiter.

    Tracks per-key call timestamps in a deque; entries older than
    `window_seconds` are evicted on each check.  Thread/task-safe via
    a single asyncio.Lock.
    """

    def __init__(self, max_calls: int, window_seconds: int = 60):
        self._max_calls = max_calls
        self._window = window_seconds
        # {key: deque of monotonic timestamps}
        self._calls: Dict[str, deque] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> bool:
        """
        Return True if the caller is within the rate limit, False if exceeded.

        Side-effect: records the current call timestamp when allowed.
        """
        try:
            async with self._lock:
                now = time.monotonic()
                bucket = self._calls.setdefault(key, deque())

                # Evict expired entries (sliding window)
                cutoff = now - self._window
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()

                if len(bucket) >= self._max_calls:
                    logger.warning(
                        "Rate limit exceeded for key=%r (%d calls in %ds window)",
                        key, len(bucket), self._window,
                    )
                    return False

                bucket.append(now)
                return True
        except Exception as exc:
            # Never block calls due to a limiter bug — fail open and log.
            logger.error("RateLimiter.check failed (failing open): %s", exc)
            return True

    def reset(self, key: str) -> None:
        """Clear the rate-limit bucket for a key (useful in tests)."""
        self._calls.pop(key, None)


# ── Module-level singleton ────────────────────────────────────────────────────

_limiter_instance: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Return the shared RateLimiter, lazily initialised from settings."""
    global _limiter_instance
    if _limiter_instance is None:
        from core.settings import get_settings
        settings = get_settings()
        _limiter_instance = RateLimiter(
            max_calls=settings.RATE_LIMIT_PER_MINUTE,
            window_seconds=60,
        )
        logger.debug(
            "RateLimiter initialised: %d calls/min", settings.RATE_LIMIT_PER_MINUTE
        )
    return _limiter_instance
