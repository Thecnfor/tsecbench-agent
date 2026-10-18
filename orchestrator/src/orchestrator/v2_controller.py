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
import os
import threading
import time
from pathlib import Path

from . import streams
from .supervisor import collect_prior
from .v2_bridge import Delegate, Finish, PlannerBridge, Wait
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

_TERMINAL_STATES = frozenset({"EXPLORED", "DEAD_END"})


def _state_name(info: dict | None) -> str | None:
    state = (info or {}).get("state")
    return getattr(state, "value", state)


class StagePlanner:
    """确定性阶段 planner：按 recon→probe→exploit→verify(→pivot) 顺序委派。

    图边是硬约束：父阶段进入终态（EXPLORED/DEAD_END）之前不委派子阶段。
    父阶段失败视为终态，避免单点失败把整条链卡死。
    """

    def __init__(
        self,
        *,
        stages: tuple[str, ...] = ("recon", "probe", "exploit", "verify"),
        directives: dict[str, str] | None = None,
        multi_flag: bool = False,
    ) -> None:
        self.stages = list(stages) + (["pivot"] if multi_flag else [])
        self.directives = {**STAGE_DIRECTIVES, **(directives or {})}

    def _delegate(self, key: str, parent: str | None) -> Delegate:
        return Delegate(key=key, directive=self.directives.get(key, ""), parent=parent)

    def next_action(self, snapshot: dict) -> Delegate | Finish | Wait:
        previous: str | None = None
        for key in self.stages:
            info = snapshot.get(key)
            if info is None or _state_name(info) == "UNEXPLORED":
                if previous is not None and _state_name(snapshot.get(previous)) not in (
                    _TERMINAL_STATES
                ):
                    return Wait(f"{previous} not finished")
                return self._delegate(key, previous)
            previous = key
        return Finish("all stages explored")


SURFACE_DIRECTIVES = {
    "probe-web": "Focus only on the web/application surface: routes, parameters, auth and "
    "session logic, injection, SSRF, upload, deserialization. Do not brute-force credentials.",
    "probe-creds": "Focus only on credential discovery and reuse: leaked secrets, weak/default "
    "logins, config/backup files, DB or admin panels, and lateral movement with any credential. "
    "Do not repeat web injection testing.",
    "exploit": "Use the strongest confirmed lead from prior assets/credentials; weaponize it "
    "to obtain the flag(s). Do not re-run broad recon.",
}


class SurfacePlanner:
    """单题双攻击面 planner：recon 串行 → 两个互斥 probe 并行 → exploit。

    与 StagePlanner 的区别只在 probe 段：两个 probe 节点同时占用并发槽
    （GraphController max_parallel>=2），各自在 node-* 子目录独立求解，
    完成后 prior 合并给 exploit。父阶段失败仍视为终态，不会卡死整链。
    """

    def __init__(
        self,
        *,
        probes: tuple[str, ...] = ("probe-web", "probe-creds"),
        directives: dict[str, str] | None = None,
    ) -> None:
        self.probes = list(probes)
        self.directives = {
            **STAGE_DIRECTIVES,
            **SURFACE_DIRECTIVES,
            **(directives or {}),
        }

    def next_action(self, snapshot: dict) -> Delegate | Finish | Wait:
        recon = snapshot.get("recon")
        if recon is None or _state_name(recon) == "UNEXPLORED":
            return Delegate("recon", self.directives["recon"], None)
        if _state_name(recon) not in _TERMINAL_STATES:
            return Wait("recon not finished")
        for key in self.probes:
            info = snapshot.get(key)
            if info is None or _state_name(info) == "UNEXPLORED":
                return Delegate(key, self.directives[key], parent="recon")
        if any(_state_name(snapshot.get(key)) not in _TERMINAL_STATES for key in self.probes):
            return Wait("probes not finished")
        exploit = snapshot.get("exploit")
        if exploit is None or _state_name(exploit) == "UNEXPLORED":
            return Delegate("exploit", self.directives["exploit"], parent=self.probes[0])
        if _state_name(exploit) not in _TERMINAL_STATES:
            return Wait("exploit not finished")
        return Finish("surfaces explored")


_PRIOR_LOCKS: dict[str, threading.Lock] = {}
_PRIOR_LOCKS_GUARD = threading.Lock()


def _prior_lock(accum: Path) -> threading.Lock:
    key = str(accum.resolve())
    with _PRIOR_LOCKS_GUARD:
        return _PRIOR_LOCKS.setdefault(key, threading.Lock())


def merge_prior_file(accum: Path, new: dict) -> None:
    """并发安全的 prior 累积：锁内读改写 + 原子替换。

    节点并行完成时「读旧-合并-写回」会互相覆盖；锁保证不丢更新，
    os.replace 保证读者永远看到完整文件。
    """
    with _prior_lock(accum):
        try:
            old = json.loads(accum.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            old = {}
        if not isinstance(old, dict):
            old = {}
        merged: dict = {}
        for field in ("assets", "explored", "open_leads", "credentials", "findings"):
            values = list(old.get(field) or []) + list(new.get(field) or [])
            merged[field] = list(dict.fromkeys(str(value) for value in values))
        tmp = accum.with_name(accum.name + ".tmp")
        accum.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, accum)


def allocate_node_slice(
    *, remaining_s: float, stages_left: int, cap_s: float, floor_s: float = 60.0
) -> float:
    """顺序阶段共享一个片预算：按剩余阶段均分，既不让前段吃光，也保底 60s。"""
    if remaining_s <= 0:
        return 0.0
    share = remaining_s / max(1, stages_left)
    return max(min(floor_s, remaining_s), min(cap_s, share))


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
    stage_count: int = 1,
    slots=None,
):
    """把 streams.run_solver_with_stream 包成节点级 runner（生产装配）。

    runner 契约（与 FakeRunner 一致）：`await runner(key=..., directive=...)`
    返回 (text, exit_code, success) 三元组；第三位真值 = 节点 EXPLORED。
    早收割 / 去重 / 计分经济学全部沿用 run_solver_with_stream 既有路径。

    阶段按图顺序串行执行，因此每个节点只拿「剩余预算 / 剩余阶段数」，
    避免 recon 吃掉整片后 probe/exploit 无预算可用。
    """
    started_nodes = 0

    async def runner(*, key: str, directive: str) -> tuple[str, int, bool]:
        nonlocal started_nodes
        node_slice = allocate_node_slice(
            remaining_s=max(0.0, deadline_epoch - time.time()),
            stages_left=max(1, stage_count - started_nodes),
            cap_s=slice_s,
        )
        started_nodes += 1
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
            slice_s=node_slice,
            environ=environ,
            client=client,
            journal=journal,
            unique_code=key,
            rec=rec,
            slots=slots,
            slot_owner=rec.unique_code,
        )
        # 节点 flags 回传父 workspace，供片尾 harvest 统一收割/去重。
        dest = ws / "out" / "flags"
        dest.mkdir(parents=True, exist_ok=True)
        for flag_file in (node_dir / "out" / "flags").glob("*.txt"):
            (dest / f"{key}_{flag_file.name}").write_text(
                flag_file.read_text(encoding="utf-8"), encoding="utf-8"
            )
        # prior 累积：下一节点 task.json 带上前面节点的资产/凭据/线索。
        merge_prior_file(accum, collect_prior(node_dir))
        return str(outcome), outcome.exit_code, not outcome.timed_out and outcome.exit_code == 0

    return runner


class V2Controller:
    def __init__(
        self,
        *,
        runner,
        max_parallel: int = 3,
        node_timeout_s: float = 900.0,
        memory=None,
        slots=None,
    ) -> None:
        from .v2_state import GraphController

        self.controller = GraphController(max_parallel=max_parallel, node_timeout_s=node_timeout_s)
        self.bridge = PlannerBridge(memory=memory, controller=self.controller)
        self.runner = runner
        self.slots = slots  # 全局 solver 进程 Semaphore（含普通片）
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
                    if isinstance(action, Wait):
                        # 父阶段仍在 EXPLORING：本拍不再派新节点，等它完成
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
        stage_count = len(getattr(planner, "stages", ()) or ()) or 1
        runner = make_stream_runner(
            solver_cmd=solver_cmd,
            base_environ=base_environ,
            slice_s=slice_s,
            ws=ws,
            client=client,
            journal=journal,
            rec=rec,
            deadline_epoch=deadline_epoch,
            stage_count=stage_count,
            slots=self.slots,
        )
        self.runner = runner
        await self._drive(planner)

    async def run_for_test(self, *, planner) -> None:
        await self._drive(planner)
