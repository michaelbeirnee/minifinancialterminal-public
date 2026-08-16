"""Function-level caching for provider calls.

Free data sources are rate-limited, so anything that leaves the process is
memoised. TTLs are graded by how fast the underlying data actually moves.
"""
from __future__ import annotations

import functools
from typing import Any, Callable, Optional, TypeVar

from ..cache import cache

# Seconds. Quotes go stale in minutes; a CIK-to-ticker map does not.
TTL_QUOTE = 120
TTL_INTRADAY = 600
TTL_DAILY = 3_600
TTL_FUNDAMENTAL = 86_400
TTL_REFERENCE = 604_800

F = TypeVar("F", bound=Callable[..., Any])


def cached(prefix: str, ttl: Optional[int] = TTL_DAILY) -> Callable[[F], F]:
    """Memoise a provider function on its arguments."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = cache.make_key(prefix, func.__name__, args, sorted(kwargs.items()))
            hit = cache.get(key, ttl=ttl)
            if hit is not None:
                return hit
            value = func(*args, **kwargs)
            if value is not None:
                cache.set(key, value)
            return value

        return wrapper  # type: ignore[return-value]

    return decorator
