"""Enkel rate limiting med Redis (glidande fönster) och minnesfallback.

Utan Redis (tom DRIVELOG_REDIS_URL, enprocessläget) används minnet direkt: det räcker
eftersom hela appen då körs i en enda process.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque

from app.config import get_config

_lock = threading.Lock()
_memory: dict[str, deque[float]] = defaultdict(deque)
_redis_client = None
_redis_failed_at = 0.0


def _redis():
    global _redis_client, _redis_failed_at
    cfg = get_config()
    # Testsviten och enprocessläget (tom DRIVELOG_REDIS_URL) använder bara minnet direkt.
    if cfg.environment == "test" or not cfg.redis_enabled:
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


# Prövar alla nycklar och registrerar försöket i dem alla bara om ingen är full, i ett steg.
_TRY_ACQUIRE_LUA = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
for _, k in ipairs(KEYS) do
  redis.call('ZREMRANGEBYSCORE', k, 0, now - window)
  if redis.call('ZCARD', k) >= limit then
    return 0
  end
end
for _, k in ipairs(KEYS) do
  redis.call('ZADD', k, now, ARGV[4])
  redis.call('EXPIRE', k, window)
end
return 1
"""


def try_acquire(keys: list[str], limit: int, window_seconds: int) -> tuple[bool, int]:
    """Prövar och registrerar ett försök i samma låsta steg.

    Försöket registreras i alla nycklar om ingen av dem har nått gränsen, annars i ingen
    (ett avvisat försök förlänger alltså inte spärren). Returnerar (tillåtet, sekunder att vänta).
    """
    now = time.time()
    client = _redis()
    if client is not None:
        try:
            rkeys = [f"drivelog:rl:{k}" for k in keys]
            member = f"{now}:{uuid.uuid4().hex}"
            ok = client.eval(_TRY_ACQUIRE_LUA, len(rkeys), *rkeys, now, window_seconds, limit, member)
            return (True, 0) if int(ok) == 1 else (False, window_seconds)
        except Exception:  # noqa: S110 - faller tillbaka till minnet
            pass
    with _lock:
        queues = [_memory[k] for k in keys]
        wait = 0
        for q in queues:
            while q and q[0] < now - window_seconds:
                q.popleft()
            if len(q) >= limit:
                wait = max(wait, int(window_seconds - (now - q[0])) + 1)
        if wait:
            return False, wait
        for q in queues:
            q.append(now)
        return True, 0


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
