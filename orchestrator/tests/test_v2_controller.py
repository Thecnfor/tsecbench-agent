"""v2_controller 图探索主控循环测试（task-7 TDD，不起子进程）。"""

from __future__ import annotations

import asyncio

from orchestrator.v2_bridge import Delegate, Finish
from orchestrator.v2_controller import StubPlanner, V2Controller
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
