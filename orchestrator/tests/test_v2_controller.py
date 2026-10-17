"""v2_controller 图探索主控循环测试（task-7 TDD，不起子进程）。"""

from __future__ import annotations

import asyncio
import json
import threading

from orchestrator.v2_bridge import Delegate, Finish, Wait
from orchestrator.v2_controller import (
    StubPlanner,
    V2Controller,
    allocate_node_slice,
    merge_prior_file,
)
from orchestrator.v2_state import DEAD_END, EXPLORED


class ScriptedPlanner:
    """按脚本吐 Delegate，验证 controller 与 bridge 的联动时序。"""

    def __init__(self, script: list[Delegate | Finish]):
        self.script = list(script)
        self.seen_snapshots: list[dict] = []

    def next_action(self, snapshot: dict) -> Delegate | Finish:
        self.seen_snapshots.append(snapshot)
        return self.script.pop(0) if self.script else Finish("script done")


class FakeRunner:
    """替代 run_solver_with_stream 的最小桩：记录委派的 key，秒回成功。"""

    def __init__(self):
        self.ran: list[str] = []

    async def __call__(self, *, key, directive, **kw):
        self.ran.append(key)
        return ("ok", 0, True)


class TestControllerLoop:
    def test_linear_chain(self):
        runner = FakeRunner()
        planner = ScriptedPlanner(
            [
                Delegate(key="recon", directive="d1"),
                Delegate(key="probe", directive="d2", parent="recon"),
                Finish("done"),
            ]
        )
        ctl = V2Controller(runner=runner, max_parallel=3, node_timeout_s=900)
        asyncio.run(ctl.run_for_test(planner=planner))
        assert runner.ran == ["recon", "probe"]
        states = {k: v["state"] for k, v in ctl.controller.snapshot().items()}
        assert states == {"recon": EXPLORED, "probe": EXPLORED}

    def test_timeout_forces_dead_end_and_releases_slot(self):
        class TimeoutRunner(FakeRunner):
            async def __call__(self, *, key, directive, **kw):
                await asyncio.sleep(0.05)
                return ("timeout", 1, False)

        planner = ScriptedPlanner(
            [
                Delegate(key="a", directive="x"),
                Delegate(key="b", directive="y"),
            ]
        )
        ctl = V2Controller(runner=TimeoutRunner(), max_parallel=1, node_timeout_s=0.01)
        asyncio.run(ctl.run_for_test(planner=planner))
        states = {k: v["state"] for k, v in ctl.controller.snapshot().items()}
        assert all(s in (EXPLORED, DEAD_END) for s in states.values())
        # 超时把槽释放出来：b 必须真的被跑过
        assert "b" in states


class TestStubPlanner:
    def test_bfs_then_finish(self):
        runner = FakeRunner()
        ctl = V2Controller(runner=runner, max_parallel=3, node_timeout_s=900)
        # 预置两个 UNEXPLORED 节点（planner 只委派既有 UNEXPLORED）
        ctl.controller.add_node("root")
        ctl.controller.add_node("child", parent="root")
        asyncio.run(ctl.run_for_test(planner=StubPlanner()))
        assert set(runner.ran) == {"root", "child"}
        states = {k: v["state"] for k, v in ctl.controller.snapshot().items()}
        assert states == {"root": EXPLORED, "child": EXPLORED}


class TestReDelegate:
    def test_redelegate_explored_node_does_not_crash_loop(self):
        """planner 重复委派已 EXPLORED 节点：跳过不炸，后续委派照常执行。"""
        runner = FakeRunner()
        planner = ScriptedPlanner(
            [
                Delegate(key="recon", directive="d1"),
                Delegate(key="recon", directive="d1-again"),  # 重复委派
                Delegate(key="probe", directive="d2", parent="recon"),
                Finish("done"),
            ]
        )
        ctl = V2Controller(runner=runner, max_parallel=3, node_timeout_s=900)
        asyncio.run(ctl.run_for_test(planner=planner))
        assert runner.ran == ["recon", "probe"]  # probe 未受重复委派影响
        assert ctl.skipped_delegates == 1
        states = {k: v["state"] for k, v in ctl.controller.snapshot().items()}
        assert states == {"recon": EXPLORED, "probe": EXPLORED}


def test_stage_planner_waits_for_exploring_parent():
    """图边是硬约束：父阶段还在 EXPLORING 时不得委派子阶段。"""
    from orchestrator.v2_controller import StagePlanner
    from orchestrator.v2_state import NodeState

    planner = StagePlanner()
    first = planner.next_action({})
    assert isinstance(first, Delegate) and first.key == "recon" and first.parent is None
    snapshot = {
        "recon": {"state": NodeState.EXPLORED, "parent": None},
        "probe": {"state": NodeState.EXPLORING, "parent": "recon"},
    }
    action = planner.next_action(snapshot)
    assert isinstance(action, Wait)
    done = {key: {"state": NodeState.EXPLORED, "parent": None} for key in planner.stages}
    assert isinstance(planner.next_action(done), Finish)


def test_stage_planner_advances_when_parent_dead_end():
    """父阶段失败（DEAD_END）是终态：不得让整条链卡死。"""
    from orchestrator.v2_bridge import Delegate
    from orchestrator.v2_controller import StagePlanner
    from orchestrator.v2_state import NodeState

    planner = StagePlanner()
    snapshot = {"recon": {"state": NodeState.DEAD_END, "parent": None}}
    action = planner.next_action(snapshot)
    assert isinstance(action, Delegate) and action.key == "probe"
    assert action.parent == "recon"


class TestPriorMerge:
    def test_concurrent_merges_keep_every_write(self, tmp_path):
        """无锁读改写会丢更新：并发完成的节点必须全部入账。"""
        accum = tmp_path / "v2_prior.json"
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                merge_prior_file(accum, {"assets": [f"10.0.0.{index}:80"]})
            except BaseException as exc:  # noqa: BLE001 - 线程异常要带回主线程断言
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 9)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert not errors
        merged = json.loads(accum.read_text(encoding="utf-8"))
        assert sorted(merged["assets"]) == [f"10.0.0.{i}:80" for i in range(1, 9)]


class TestNodeSliceBudget:
    def test_even_split_across_remaining_stages(self):
        assert allocate_node_slice(remaining_s=400, stages_left=4, cap_s=900) == 100

    def test_floor_never_exceeds_remaining(self):
        assert allocate_node_slice(remaining_s=20, stages_left=4, cap_s=900) == 20

    def test_cap_wins_on_short_chains(self):
        assert allocate_node_slice(remaining_s=4000, stages_left=2, cap_s=900) == 900

    def test_no_budget_left(self):
        assert allocate_node_slice(remaining_s=0, stages_left=4, cap_s=900) == 0


def test_stage_planner_adds_pivot_for_multi_flag():
    from orchestrator.v2_controller import StagePlanner

    planner = StagePlanner(multi_flag=True)
    assert planner.stages[-1] == "pivot"
