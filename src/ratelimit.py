"""进程内限流：保护登录/管理接口不被爆破，也顺便挡住扫描器。

单进程单 worker 部署，内存计数足够；重启清零可接受。
"""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """滑动窗口限流。key 通常是 "ip:scope"。"""

    def __init__(self, max_hits: int = 20, window_sec: float = 60.0, block_sec: float = 120.0):
        self.max_hits = max_hits
        self.window = window_sec
        self.block = block_sec
        self._hits: dict[str, deque] = {}
        self._blocked: dict[str, float] = {}
        self._lock = threading.Lock()
        self._last_gc = time.time()

    def _gc(self, now: float) -> None:
        if now - self._last_gc < 60:
            return
        self._last_gc = now
        for k in [k for k, v in self._blocked.items() if v <= now]:
            self._blocked.pop(k, None)
        for k, dq in list(self._hits.items()):
            while dq and dq[0] <= now - self.window:
                dq.popleft()
            if not dq:
                self._hits.pop(k, None)

    def blocked(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            until = self._blocked.get(key)
            if until and until > now:
                return True
            if until:
                self._blocked.pop(key, None)
            return False

    def hit(self, key: str) -> bool:
        """记一次失败。返回 True 表示本次起已被封禁。"""
        now = time.time()
        with self._lock:
            self._gc(now)
            dq = self._hits.setdefault(key, deque())
            dq.append(now)
            while dq and dq[0] <= now - self.window:
                dq.popleft()
            if len(dq) >= self.max_hits:
                self._blocked[key] = now + self.block
                dq.clear()
                return True
            return False

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)
            self._blocked.pop(key, None)