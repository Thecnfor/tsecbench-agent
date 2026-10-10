"""PlatformClient 单测 —— 对着 mock server 走真实 HTTP 路径，验证错误分派与重试。"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from mock_server.server import make_server
from mock_server.state import ChallengeSpec
from orchestrator.api import (
    ChallengeGone,
    Duplicate,
    FlagRejected,
    HintCompleted,
    LevelLocked,
    PlatformClient,
    ResourceUnavailable,
    RetryableError,
    StartFull,
    TaskEnded,
)

TOKEN = "mock-token"
FAST = dict(
    retries_429=1,
    retries_500=1,
    retries_network=1,
    delay_429=0.01,
    delay_500=0.01,
    delay_network=0.01,
    backoff_503=(0.01, 0.02),
    jitter=0.0,
)


@pytest.fixture
def factory():
    servers: list = []

    def make(specs=None, env=None):
        server, state = make_server(specs, env=env, token=TOKEN)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}", state

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


def client(base: str) -> PlatformClient:
    return PlatformClient(base, TOKEN, qps=100.0, **FAST)


def run(coro):
    return asyncio.run(coro)


# ---- 正常 5 接口 ----
def test_full_five_interfaces(factory):
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"])])
    c = client(base)
    rows = run(c.list_challenges())
    assert rows[0]["unique_code"] == "c1"
    assert run(c.start("c1"))["container_status"] in ("pending", "available")

    async def wait_available():
        for _ in range(50):
            rows = await c.list_challenges()
            if rows[0]["container_status"] == "available":
                return rows[0]
            await asyncio.sleep(0.05)
        raise AssertionError("never available")

    row = run(wait_available())
    assert row["container_addr"]
    assert run(c.hint("c1")) == "mock hint for c1"  # 通关前取 hint 正常
    result = run(c.submit("c1", "FLAG{a}"))
    assert result["matched_flag_index"] == 0
    assert run(c.close("c1"))["container_status"] == "stopped"


def test_submit_wrong_flag_progress_true(factory):
    """错误 flag：HTTP 200 但 matched_flag_index 为 null —— 进度真值在 correct_flag_count。"""
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"])])
    c = client(base)
    run(c.start("c1"))
    result = run(c.submit("c1", "FLAG{wrong}"))
    assert result["matched_flag_index"] is None
    assert result["correct_flag_count"] == 0


def test_duplicate_exception(factory):
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"])])
    c = client(base)
    run(c.start("c1"))
    run(c.submit("c1", "FLAG{a}"))
    with pytest.raises(Duplicate):
        run(c.submit("c1", "FLAG{a}"))


# ---- 404 分派 ----
def test_task_not_found_raises_task_ended(factory):
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"])])
    c = PlatformClient(base, "wrong-token", qps=100.0, **FAST)
    with pytest.raises(TaskEnded):
        run(c.list_challenges())


def test_challenge_not_found(factory):
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"])])
    c = client(base)
    with pytest.raises(ChallengeGone):
        run(c.start("nope"))


# ---- 409 三义性分派（按接口，不按错误码）----
def test_start_409_is_start_full(factory):
    specs = [ChallengeSpec(f"c{i}", flags=["FLAG{a}"]) for i in range(4)]
    base, _ = factory(specs)
    c = client(base)
    for i in range(3):
        run(c.start(f"c{i}"))
    with pytest.raises(StartFull):
        run(c.start("c3"))


def test_hint_on_completed_is_hint_completed(factory):
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"])])
    c = client(base)
    run(c.start("c1"))
    run(c.submit("c1", "FLAG{a}"))
    with pytest.raises(HintCompleted):
        run(c.hint("c1"))


def test_task_timeout_is_task_ended_from_list(factory):
    """任务级超时后 GET /challenges 也 409 → TaskEnded（全局停机信号）。"""
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"])], env={"MOCK_TOTAL_LIMIT_S": "0.2"})
    c = client(base)
    time.sleep(0.35)
    with pytest.raises(TaskEnded):
        run(c.list_challenges())


# ---- 422 / 403 / 503 / 500 / 畸形体 ----
def test_422_flag_rejected(factory):
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"])])
    c = client(base)
    run(c.start("c1"))
    with pytest.raises(FlagRejected):
        run(c.submit("c1", ""))


def test_403_level_locked(factory):
    specs = [
        ChallengeSpec("easy", flags=["FLAG{e}"]),
        ChallengeSpec("hard", level=2, flags=["FLAG{h}"]),
    ]
    base, _ = factory(specs, env={"MOCK_LEVEL_GATE": "on", "MOCK_CURRENT_LEVEL": "1"})
    c = client(base)
    with pytest.raises(LevelLocked):
        run(c.start("hard"))


def test_503_backoff_then_success(factory):
    """503 按退避序列重试，恢复后成功 —— 不抛 ResourceUnavailable。"""
    base, _ = factory(
        [ChallengeSpec("c1", flags=["FLAG{a}"], faults={"start": ["503", "503", "ok"]})]
    )
    c = client(base)
    result = run(c.start("c1"))
    assert result["container_status"] in ("pending", "available")


def test_503_exhausted_raises_resource_unavailable(factory):
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"], faults={"start": ["503"]})])
    c = client(base)
    with pytest.raises(ResourceUnavailable):
        run(c.start("c1"))


def test_500_retries_then_success(factory):
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"], faults={"start": ["500", "ok"]})])
    c = client(base)
    assert run(c.start("c1"))["container_status"] in ("pending", "available")


def test_garbage_body_retries_then_raises(factory):
    """200 + 畸形体：重试耗尽后 RetryableError，不崩。"""
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"], faults={"start": ["garbage"]})])
    c = client(base)
    with pytest.raises(RetryableError):
        run(c.start("c1"))


def test_reset_connection_retries(factory):
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"], faults={"start": ["reset", "ok"]})])
    c = client(base)
    assert run(c.start("c1"))["container_status"] in ("pending", "available")  # 网络错误重试后成功


# ---- 全局限速 ----
def test_token_bucket_enforces_qps(factory):
    """qps=3：10 个请求至少耗 ~3 秒（9 次等待 / 3 qps）。"""
    base, _ = factory([ChallengeSpec("c1", flags=["FLAG{a}"])])
    c = PlatformClient(base, TOKEN, qps=3.0, **FAST)
    start = time.monotonic()

    async def burst():
        return await asyncio.gather(*(c.list_challenges() for _ in range(10)))

    run(burst())
    elapsed = time.monotonic() - start
    assert elapsed >= 2.0  # 10 req / 3 qps ≈ 3s，留余量
