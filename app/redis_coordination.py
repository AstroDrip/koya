"""Redis acceleration with bounded fallback to the authoritative database."""

import asyncio
import os
import threading
import time
from weakref import WeakKeyDictionary

CHANNEL = "koya:ops:wakeup"
RATE_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], 65) end
return count
"""
_clients = WeakKeyDictionary()
_local_rates = {}
_mutex = threading.Lock()


def redis_url():
    return os.getenv("MRCARE_REDIS_URL", "").strip()


def async_client():
    url = redis_url()
    if not url:
        return None
    from redis.asyncio import Redis

    loop = asyncio.get_running_loop()
    with _mutex:
        clients = _clients.setdefault(loop, {})
        if url not in clients:
            clients[url] = Redis.from_url(url, socket_connect_timeout=.25, socket_timeout=1.5, max_connections=128)
        return clients[url]


def sync_client():
    url = redis_url()
    if not url:
        return None
    from redis import Redis

    return Redis.from_url(url, socket_connect_timeout=.25, socket_timeout=.4)


async def rate_allowed(scope, limit):
    minute = int(time.time()) // 60
    client = async_client()
    if client is not None:
        from redis.exceptions import RedisError

        try:
            async with asyncio.timeout(.4):
                count = await client.eval(RATE_SCRIPT, 1, f"koya:rate:{scope}:{minute}")
                return count <= limit, True
        except (RedisError, TimeoutError):
            pass
    with _mutex:
        bucket, count = _local_rates.get(scope, (minute, 0))
        count = count + 1 if bucket == minute else 1
        _local_rates[scope] = (minute, count)
    return count <= limit, False
