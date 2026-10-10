"""全局令牌桶单测 —— ≤3 req/s、突发容量、并发总量上限（runbook.md §4.2 出站 QPS 用例）。"""

import asyncio
import time

import pytest
from orchestrator.ratelimit import TokenBucket


class TestParams:
    def test_invalid_params_rejected(self) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate=0)
        with pytest.raises(ValueError):
            TokenBucket(capacity=-1)

    def test_cost_over_capacity_rejected(self) -> None:
        async def run() -> None:
            bucket = TokenBucket(rate=3, capacity=2)
            with pytest.raises(ValueError):
                await bucket.acquire(3)

        asyncio.run(run())


class TestBehavior:
    def test_burst_immediate_up_to_capacity(self) -> None:
        async def run() -> None:
            bucket = TokenBucket(rate=3, capacity=3)
            start = time.monotonic()
            for _ in range(3):
                await bucket.acquire()
            assert time.monotonic() - start < 0.1  # 满桶突发不等待

        asyncio.run(run())

    def test_fourth_request_waits(self) -> None:
        async def run() -> None:
            bucket = TokenBucket(rate=10, capacity=3)  # 高 rate 隔离「回填快」因素
            for _ in range(3):
                await bucket.acquire()
            start = time.monotonic()
            await bucket.acquire()
            elapsed = time.monotonic() - start
            assert 0.05 < elapsed < 0.5  # 必须等回填，但 10/s 下不超过一个令牌的周期

        asyncio.run(run())

    def test_sustained_rate_not_exceeded(self) -> None:
        async def run() -> None:
            rate = 20.0
            bucket = TokenBucket(rate=rate, capacity=3)
            start = time.monotonic()
            n = 10
            for _ in range(n):
                await bucket.acquire()
            elapsed = time.monotonic() - start
            # 10 个请求、容量 3 → 至少要回填 7 个 → ≥ 7/20 s；且不超过理论值的宽松上界
            assert elapsed >= (n - 3) / rate - 0.02
            assert elapsed < (n / rate) + 1.0

        asyncio.run(run())

    def test_concurrent_total_not_exceeded(self) -> None:
        """3 个协程同时抢 8 个令牌（容量 3），总量守恒 —— 对应「3 题并发批量 submit」。"""

        async def run() -> None:
            bucket = TokenBucket(rate=50, capacity=3)
            done = 0
            gate = asyncio.Barrier(8)

            async def worker() -> None:
                nonlocal done
                await gate.wait()
                await bucket.acquire()
                done += 1

            start = time.monotonic()
            await asyncio.gather(*(worker() for _ in range(8)))
            assert done == 8
            assert time.monotonic() - start >= (8 - 3) / 50 - 0.02

        asyncio.run(run())
