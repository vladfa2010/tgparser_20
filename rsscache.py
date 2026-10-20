"""In-memory TTL-кеш для RSS-ответов.

RSS-ридеры (Пульс и др.) поллят фиды регулярно; без кеша каждый полл — запрос
с сортировкой к PostgreSQL. Кеш живёт 120 с, инвалидируется при любых правках
в админке (admin.py вызывает invalidate()), поэтому изменения состава/названий
фидов видны сразу, а свежие посты — не позднее TTL.
"""
import time
from typing import Any, Awaitable, Callable

TTL_SEC = 120
_store: dict[str, tuple[float, Any]] = {}


async def get(key: str, producer: Callable[[], Awaitable[Any]]) -> Any:
    now = time.monotonic()
    entry = _store.get(key)
    if entry and now - entry[0] < TTL_SEC:
        return entry[1]
    value = await producer()
    _store[key] = (now, value)
    return value


def invalidate() -> None:
    _store.clear()
