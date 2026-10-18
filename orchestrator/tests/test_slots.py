"""并发槽计数：只认在飞 pipeline，不能与 registry 对账视图相加。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from orchestrator.main import _active_slots


def test_deep_task_detection():
    from orchestrator.main import _is_deep_task

    def rec(**kw):
        base = dict(flag_count=1, difficulty="easy", description="", container_addr=["10.0.0.1:80"])
        base.update(kw)
        return SimpleNamespace(**base)

    assert _is_deep_task(rec(description="内部门户穿透评估"))
    assert _is_deep_task(rec(container_addr=["10.0.0.1:23"]))  # telnet 需要长跑道
    assert not _is_deep_task(rec(description="文档协作平台"))


def test_fair_slot_pool_prevents_pipeline_starvation():
    import asyncio

    from orchestrator.streams import FairSlotPool

    async def scenario():
        pool = FairSlotPool(3)
        for code in ("a", "b", "c"):
            pool.register(code)
        await pool.acquire("a")
        second = asyncio.create_task(pool.acquire("a"))
        await asyncio.sleep(0.01)
        assert not second.done()  # b/c 还没有槽，a 不能开第二个
        await pool.acquire("b")
        await pool.acquire("c")
        await pool.release("c")
        await pool.unregister("c")  # c 的 pipeline 结束后，a 才能开第二个
        await asyncio.wait_for(second, 0.5)
        assert pool.used == 3
        await pool.release("a")
        await pool.release("b")
        await pool.release("a")

    asyncio.run(scenario())


def test_active_slots_counts_running_pipelines_only():
    ctx = SimpleNamespace(running={"a", "b"}, registry=SimpleNamespace(active_count=lambda: 99))
    assert _active_slots(ctx) == 2


def test_active_slots_frees_when_pipeline_finishes():
    running = {"a", "b", "c"}
    ctx = SimpleNamespace(running=running, registry=SimpleNamespace(active_count=lambda: 3))
    running.remove("a")
    assert _active_slots(ctx) == 2


def test_should_extend_rules():
    from orchestrator.main import _should_extend

    assert _should_extend(240, False, True) is True
    assert _should_extend(0, False, True) is False
    assert _should_extend(240, True, True) is False
    assert _should_extend(240, False, False) is False


def test_probe_decision_tiers():
    from orchestrator.main import _probe_decision

    assert _probe_decision(1, True, False, 240) == "second"
    assert _probe_decision(1, False, False, 240) == "stop"
    assert _probe_decision(2, True, False, 240) == "full"
    assert _probe_decision(2, True, True, 240) == "full"
    assert _probe_decision(3, True, False, 240) == "full"
    assert _probe_decision(1, True, True, 0) == "stop"
    # deep 题：首轮无进展也必须走完两段探测 + 剩余片（多阶段横向需要跑道）
    assert _probe_decision(1, False, False, 240, deep=True) == "second"
    assert _probe_decision(2, False, False, 240, deep=True) == "full"


def test_pass_progress_only_counts_flag_candidates(tmp_path):
    from orchestrator.main import _pass_progress

    ws = tmp_path / "ws"
    (ws / "out").mkdir(parents=True)
    (ws / "out" / "prior.json").write_text(
        json.dumps({"credentials": ["Passw0rd!123"], "assets": ["10.0.0.1:80"]}),
        encoding="utf-8",
    )
    report = SimpleNamespace(new_matches=0)
    assert _pass_progress(report, 0, ws) is False
    (ws / "out" / "flags").mkdir()
    (ws / "out" / "flags" / "000.txt").write_text(
        "flag{93a3880a-5dfa-4bcb-8ee7-ea93bac9d214}", encoding="utf-8"
    )
    assert _pass_progress(report, 0, ws) is True
