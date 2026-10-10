"""全局令牌桶限速器（architecture.md §5.1）。

所有出站平台 API 请求必须过同一个桶：默认 ≤3 req/s（上届规则，本届未提但必须防）。
3 题并发时三个 solver 的收割结果可能同时触发 submit，加上 Registry 对账轮询，
瞬时 QPS 很容易超限 —— 限速是 API Client 的一部分，不是可选项。

asyncio 实现：L0 是单进程事件循环（architecture.md §1.1），无需线程安全。
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """经典令牌桶：capacity 个突发额度，按 rate 个/秒匀速回填。"""

    def __init__(self, rate: float = 3.0, capacity: float = 3.0) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self._rate = float(rate)
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    async def acquire(self, cost: float = 1.0) -> None:
        """取走 cost 个令牌；不足时挂起直到够用。"""
        if cost > self._capacity:
            raise ValueError("cost exceeds bucket capacity")
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                deficit = cost - self._tokens
                await asyncio.sleep(deficit / self._rate)
