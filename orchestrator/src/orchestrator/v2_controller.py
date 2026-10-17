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
import contextlib
import json

from . import streams
from .supervisor import collect_prior
from .v2_bridge import Delegate, Finish, PlannerBridge
from .v2_state import IllegalTransition, SlotFullError


class StubPlanner:
    """确定性假 planner：广度优先委派 UNEXPLORED 节点，全部过一遍即 Finish。"""

    def next_action(self, snapshot: dict) -> Delegate | Finish:
        for key, info in snapshot.items():
            if info["state"].value == "UNEXPLORED":
                return Delegate(key=key, directive="", parent=info["parent"])
        return Finish("all visited")


STAGE_DIRECTIVES = {
    "recon": "Map every reachable service, endpoint and technology; record assets.",
    "probe": "Test each surface against likely weakness classes; pick the strongest lead.",
    "exploit": "Weaponize the strongest lead into a working exploit and collect the flag.",
    "verify": "Confirm the flag exactly as found; do not invent or transform it.",
    "pivot": "Reuse confirmed credentials/assets to reach the next stage; find remaining flags.",
}


class StagePlanner:
    """确定性阶段 planner：按 recon→probe→exploit→verify(→pivot) 顺序委派。"""

    def __init__(
        self,
        *,
        stages: tuple[str, ...] = ("recon", "probe", "exploit", "verify"),
        directives: dict[str, str] | None = None,
        multi_flag: bool = False,
    ) -> None:
        self.stages = list(stages) + (["pivot"] if multi_flag else [])
        self.directives = {**STAGE_DIRECTIVES, **(directives or {})}

    def next_action(self, snapshot: dict) -> Delegate | Finish:
        previous: str | None = None
        for key in self.stages:
            info = snapshot.get(key)
            if info is None:
                return Delegate(key=key, directive=self.directives.get(key, ""), parent=previous)
            if info["state"].value == "UNEXPLORED":
                return Delegate(key=key, directive=self.directives.get(key, ""), parent=previous)
            previous = key
        return Finish("all stages explored")


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
        (node_dir / "out" / "flags").mkdir(parents=True, exist_ok=True)
        task: dict = {}
        try:
            task = json.loads((ws / "task.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            task = {}
        task = dict(task)
        if directive:
            task["description"] = (
                str(task.get("description") or "") + f"\n\n# NODE FOCUS\n{directive}"
            ).strip()
        accum = ws / "out" / "v2_prior.json"
        with contextlib.suppress(OSError, ValueError):
            task["prior"] = json.loads(accum.read_text(encoding="utf-8"))
        (node_dir / "task.json").write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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
        # 节点 flags 回传父 workspace，供片尾 harvest 统一收割/去重。
        dest = ws / "out" / "flags"
        dest.mkdir(parents=True, exist_ok=True)
        for flag_file in (node_dir / "out" / "flags").glob("*.txt"):
            (dest / f"{key}_{flag_file.name}").write_text(
                flag_file.read_text(encoding="utf-8"), encoding="utf-8"
            )
        # prior 累积：下一节点 task.json 带上前面节点的资产/凭据/线索。
        try:
            old = json.loads(accum.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        new = collect_prior(node_dir)
        merged: dict = {}
        for field in ("assets", "explored", "open_leads", "credentials", "findings"):
            values = list(old.get(field) or []) + list(new.get(field) or [])
            merged[field] = list(dict.fromkeys(str(value) for value in values))
        accum.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
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
        self.skipped_delegates = 0  # planner 重复/越权委派被跳过的计数（观测位）

    async def _drive(self, planner, *, poll_interval: float = 0.01) -> None:
        loop = asyncio.get_running_loop()
        running: dict[str, asyncio.Task] = {}
        finished = False
        try:
            while True:
                # 熔断扫描：到点节点标 DEAD_END、释放槽，并取消其在跑 task
                for key in self.controller.expired():
                    self.bridge.confirm_state(key, explored=False)
                    task = running.pop(key, None)
                    if task is not None:
                        task.cancel()
                # 空槽 + planner 决策（Finish 后不再派新活，只排空在跑的）
                while not finished and self.controller.free_slots > 0:
                    action = await loop.run_in_executor(
                        None, planner.next_action, self.controller.snapshot()
                    )
                    if isinstance(action, Finish):
                        finished = True
                        break
                    try:
                        self.bridge.handle_delegate(action)
                        self.controller.begin(action.key)
                    except IllegalTransition:
                        # planner 重复委派已入图节点（add_node 幂等后 begin 对
                        # 非 UNEXPLORED 抛错）或 parent 未知：跳过该节点、不炸
                        # 主循环（确定性——在跑/已完成节点不受影响）。
                        self.skipped_delegates += 1
                        continue
                    except SlotFullError:
                        # begin 前置检查 free_slots 与此处之间无并发写入者，
                        # 正常不会触发；若触发视为「本轮无槽」，下一拍重试。
                        break
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
