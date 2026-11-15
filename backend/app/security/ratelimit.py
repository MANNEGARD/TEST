"""Enkel rate limiting med Redis (glidande fönster) och minnesfallback."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from app.config import get_config

_lock = threading.Lock()
_memory: dict[str, deque[float]] = defaultdict(deque)
_redis_client = None
_redis_failed_at = 0.0


def _redis():
    global _redis_client, _redis_failed_at
    cfg = get_config()
    if cfg.environment == "test":
        return None
    if _redis_client is not None:
        return _redis_client
    if time.time() - _redis_failed_at < 30:
        return None
    try:
        import redis

        client = redis.Redis.from_url(cfg.redis_url, socket_connect_timeout=0.5, socket_timeout=0.5)
        client.ping()
        _redis_client = client
        return client
    except Exception:
        _redis_failed_at = time.time()
        return None


def hit(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Registrerar ett försök. Returnerar (tillåtet, sekunder att vänta)."""
    now = time.time()
    client = _redis()
    if client is not None:
        try:
            rkey = f"drivelog:rl:{key}"
            pipe = client.pipeline()
            pipe.zremrangebyscore(rkey, 0, now - window_seconds)
            pipe.zadd(rkey, {f"{now}": now})
            pipe.zcard(rkey)
            pipe.expire(rkey, window_seconds)
            _, _, count, _ = pipe.execute()
            if count > limit:
                return False, window_seconds
            return True, 0
        except Exception:  # noqa: S110 - faller tillbaka till minnet
            pass
    with _lock:
        q = _memory[key]
        while q and q[0] < now - window_seconds:
            q.popleft()
        q.append(now)
        if len(q) > limit:
            return False, int(window_seconds - (now - q[0])) + 1
        return True, 0


def check(key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Prövar om ett nytt försök tillåts, utan att registrera det."""
    now = time.time()
    client = _redis()
    if client is not None:
        try:
            rkey = f"drivelog:rl:{key}"
            pipe = client.pipeline()
            pipe.zremrangebyscore(rkey, 0, now - window_seconds)
            pipe.zcard(rkey)
            _, count = pipe.execute()
            return (False, window_seconds) if count >= limit else (True, 0)
        except Exception:  # noqa: S110 - faller tillbaka till minnet
            pass
    with _lock:
        q = _memory[key]
        while q and q[0] < now - window_seconds:
            q.popleft()
        if len(q) >= limit:
            return False, int(window_seconds - (now - q[0])) + 1
        return True, 0


def record(key: str, window_seconds: int) -> None:
    """Registrerar ett försök (används tillsammans med check)."""
    now = time.time()
    client = _redis()
    if client is not None:
        try:
            rkey = f"drivelog:rl:{key}"
            pipe = client.pipeline()
            pipe.zadd(rkey, {f"{now}": now})
            pipe.expire(rkey, window_seconds)
            pipe.execute()
            return
        except Exception:  # noqa: S110 - faller tillbaka till minnet
            pass
    with _lock:
        _memory[key].append(now)


def reset(key: str | None = None) -> None:
    with _lock:
        if key is None:
            _memory.clear()
        else:
            _memory.pop(key, None)
    if key is not None:
        client = _redis()
        if client is not None:
            try:
                client.delete(f"drivelog:rl:{key}")
            except Exception:  # noqa: S110 - Redis är valfritt
                pass
