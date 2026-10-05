"""共用的 TTL 缓存（AIHOT 的 `lib/cache.ts`）。

一个进程里的小缓存，有三个规矩**写在这里**，而不是每个调用方各自实现一遍：

- 过期就重算（没有“阵旧但先用着”这一档）；
- 缓存键由调用方给（哈希它自己），这一层不猜；
- 写入必须能`失效`，否则“改了没生效”会变成一个难查的故障。

线程安全: 采集在线程里跑，读取在事件循环里跑，所以加一把锁。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Hashable


class _Entry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at


class TTLCache:
    """一个进程内的过期缓存。`seconds <= 0` 等于关掉缓存。"""

    def __init__(self, seconds: float = 60.0, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._seconds = float(seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[Hashable, _Entry] = {}

    def get(self, key: Hashable, default: Any = None) -> Any:
        if self._seconds <= 0:
            return default
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return default
            if entry.expires_at <= self._clock():
                self._entries.pop(key, None)
                return default
            return entry.value

    def set(self, key: Hashable, value: Any) -> Any:
        if self._seconds <= 0:
            return value
        with self._lock:
            self._entries[key] = _Entry(value, self._clock() + self._seconds)
        return value

    def invalidate(self, key: Hashable | None = None) -> None:
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)

    def through(self, key: Hashable, build: Callable[[], Any]) -> Any:
        """缓存里有就还它；没有就调 `build()`。**不缓存异常**。"""
        missing = object()
        found = self.get(key, missing)
        if found is not missing:
            return found
        return self.set(key, build())


#: 能力位的选择结果缓存 1 分钟（AIHOT 的 `capabilities` 缓存）。
capability_cache = TTLCache(60.0)
