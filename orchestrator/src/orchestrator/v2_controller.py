"""v2 主控：图探索循环（harness-v2-openhands.md §2/§3）。

并发的唯一切实点在 GraphController；本模块只负责把
planner 意图 → 节点执行（runner 注入，生产 = streams.run_solver_with_stream
包一层，测试 = FakeRunner）→ 结果回灌。HARNESS_V2=off 时本模块不被调用。

run_task 生产装配（thin）：把 streams.run_solver_with_stream 包成 runner ——
每个委派节点在 workspace 内的 `node-{key}` 子目录独立跑（SOLVER_NODE_DIRECTIVE
env 注入 dual_main 的 APPEND_SYSTEM），早收割沿用 run_solver_with_stream 既有的
submit_candidates 路径；经济学（rec.submitted 去重 / 计分）不动。
"""

from __future__ import annotations

import asyncio

from . import streams
from .v2_bridge import Delegate, Finish, PlannerBridge


class StubPlanner:
    """确定性假 planner：广度优先委派 UNEXPLORED 节点，全部过一遍即 Finish。"""

    def next_action(self, snapshot: dict) -> Delegate | Finish:
        for key, info in snapshot.items():
            if info["state"].value == "UNEXPLORED":
                return Delegate(key=key, directive="", parent=info["parent"])
        return Finish("all visited")


def make_stream_runner(
    *,
    solver_cmd: list[str],
    base_environ: dict[str, str] | None,
    slice_s: float,
    ws,
    client,
    journal,
    rec,
    deadline_epoch: float,
):
    """把 streams.run_solver_with_stream 包成节点级 runner（生产装配）。

    runner 契约（与 FakeRunner 一致）：`await runner(key=..., directive=...)`
    返回 (text, exit_code, success) 三元组；第三位真值 = 节点 EXPLORED。
    早收割 / 去重 / 计分经济学全部沿用 run_solver_with_stream 既有路径。
    """

    async def runner(*, key: str, directive: str) -> tuple[str, int, bool]:
        node_dir = ws / f"node-{key}"
        node_dir.mkdir(parents=True, exist_ok=True)
        environ = dict(base_environ or {})
        if directive:
            environ["SOLVER_NODE_DIRECTIVE"] = directive
        outcome, _early_new, _early_completed = await streams.run_solver_with_stream(
            solver_cmd=solver_cmd,
            ws=node_dir,
            deadline_epoch=deadline_epoch,
            slice_s=slice_s,
            environ=environ,
            client=client,
            journal=journal,
            unique_code=key,
            rec=rec,
        )
        return str(outcome), outcome.exit_code, not outcome.timed_out and outcome.exit_code == 0

    return runner


class V2Controller:
    def __init__(
        self, *, runner, max_parallel: int = 3, node_timeout_s: float = 900.0, memory=None
    ) -> None:
        from .v2_state import GraphController

        self.controller = GraphController(max_parallel=max_parallel, node_timeout_s=node_timeout_s)
        self.bridge = PlannerBridge(memory=memory, controller=self.controller)
        self.runner = runner

    async def _drive(self, planner, *, poll_interval: float = 0.01) -> None:
        loop = asyncio.get_running_loop()
        running: dict[str, asyncio.Task] = {}
        finished = False
        try:
            while True:
                # 熔断扫描：到点节点标 DEAD_END 并释放槽
                for key in self.controller.expired():
                    self.bridge.confirm_state(key, explored=False)
                    running.pop(key, None)
                # 空槽 + planner 决策（Finish 后不再派新活，只排空在跑的）
                while not finished and self.controller.free_slots > 0:
                    action = await loop.run_in_executor(
                        None, planner.next_action, self.controller.snapshot()
                    )
                    if isinstance(action, Finish):
                        finished = True
                        break
                    self.bridge.handle_delegate(action)
                    self.controller.begin(action.key)
                    running[action.key] = asyncio.create_task(
                        self._run_node(action.key, action.directive)
                    )
                if finished and not running:
                    return
                if not running:
                    await asyncio.sleep(poll_interval)
                    continue
                done, _ = await asyncio.wait(
                    running.values(), timeout=poll_interval, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    key = next((k for k, task in running.items() if task is t), None)
                    if key is None:
                        continue  # 已被 expired 扫描弹出
                    explored = bool(t.result() and t.result()[2])
                    self.bridge.confirm_state(key, explored=explored)
                    running.pop(key)
        finally:
            for t in running.values():
                t.cancel()

    async def _run_node(self, key: str, directive: str):
        return await self.runner(key=key, directive=directive)

    async def run_task(
        self,
        *,
        task: str,
        ws,
        client,
        journal,
        rec,
        deadline_epoch: float,
        total_budget_s: float,
        planner,
        solver_cmd: list[str],
        base_environ: dict[str, str] | None = None,
        slice_s: float = streams.STREAM_INTERVAL_S,
    ) -> None:
        """生产入口：装配节点 runner 后进入 _drive 主循环。

        参数来源与 main.py `_pipeline` 的调用点同源（ctx.solver_cmd / ctx.env /
        prepare_workspace 的 ws / 片内 deadline）；`total_budget_s` 供 planner 侧
        预算决策，本模块不截断（deadline_epoch 才是硬闸）。节点级 env 在共享
        base_environ 之上按节点追加 SOLVER_NODE_DIRECTIVE。
        """
        del total_budget_s, task  # 预算截断由 planner/deadline 负责，此处只透传语义
        runner = make_stream_runner(
            solver_cmd=solver_cmd,
            base_environ=base_environ,
            slice_s=slice_s,
            ws=ws,
            client=client,
            journal=journal,
            rec=rec,
            deadline_epoch=deadline_epoch,
        )
        self.runner = runner
        await self._drive(planner)

    async def run_for_test(self, *, planner) -> None:
        await self._drive(planner)
