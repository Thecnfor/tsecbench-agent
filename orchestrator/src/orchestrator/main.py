"""编排器主循环（architecture.md §3.1 状态机 / §4.3 两阶段预算 / §3.2 崩溃恢复）。

CLI 入口：`python -m orchestrator`。
对 mock 可全链路真实测试：list → start → poll available → solver → harvest →
submit → close，含 LOCKED 重入队、收尾保护、崩溃恢复对账。

**hint 接口已整体移除**（architecture.md §4.4）：扣分不可逆、本届读不到
`hint_cost_radio` 无法做成本判断，实测 10 次取 hint 仅 1 次换来解出 —— 期望收益
为负。删掉的是整条链路：PlatformClient.hint / Hint Broker / HINT_RETRY 状态 /
`hints_taken` 记账 / Journal 的 hint 事件与 fsync / task.json 的 `hint` 字段。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import sop as sop_mod
from . import streams
from .api import (
    ChallengeGone,
    LevelLocked,
    PlatformClient,
    RetryableError,
    StartFull,
    TaskEnded,
)
from .config import RuntimeConfig, resolve_config
from .harvester import harvest, submit_candidates
from .journal import Journal, score_summary
from .memory import AttackMemory
from .registry import ChallengeRecord, Registry
from .repair import BASE_WEIGHTS, choose_repair, update_weights
from .scheduler import can_start, eligible, plan_phase, slice_for
from .supervisor import collect_prior, prepare_workspace

POLL_INTERVAL_S = 2.0
AVAILABLE_TIMEOUT_S = 120.0
# 等 available 的轮询退避（§4.1 修订）。原先固定 0.2s：3 个 pipeline 同时轮询 =
# 15 req/s 需求，而全局令牌桶只有 3 req/s —— 轮询把桶吃满，submit/close 排在
# 轮询后面，实测每题 solver_done→solved 固定多花 ~11s（63 题 ≈ 11 分钟纯开销）。
# 改为自适应：前几拍快（容器常 1~3s 就绪），随后退避，把额度让给写操作。
AVAILABLE_POLL_START_S = 0.3
AVAILABLE_POLL_MAX_S = 2.0
AVAILABLE_POLL_GROWTH = 1.6
MAX_ATTEMPTS = 2  # 普通重试上限（失败一次后换策略再投一轮）


@dataclass(slots=True)
class RunContext:
    config: RuntimeConfig
    client: PlatformClient
    registry: Registry
    journal: Journal
    workspace_root: Path
    solver_cmd: list[str]
    priors: dict[str, dict] = field(default_factory=dict)  # code → 双图状态（跨轮合并）
    running: set[str] = field(default_factory=set)
    stopped: bool = False
    t0: float = 0.0
    env: dict[str, str] | None = None  # 传给 solver 的基础环境（测试注入用）
    # harness-v2（harness-v2.md §4/§5）：SOP 检索与修复记账；重启丢失，
    # 不参与崩溃恢复 —— 修复权重是启发式，丢了从 BASE 重新演化即可。
    repair_notes: dict[str, str] = field(default_factory=dict)  # code → 换面指令
    repair_weights: dict[str, float] = field(default_factory=lambda: dict(BASE_WEIGHTS))
    last_repair: dict[str, str] = field(default_factory=dict)  # code → 上轮修复动作
    # 攻击图记忆（harness-v2.md §6）：triviumdb 缺失即降级为空操作
    memory: AttackMemory | None = None


# ---------------------------------------------------------------- helpers ----
async def _safe_list(ctx: RunContext) -> list[dict] | None:
    try:
        return await ctx.client.list_challenges()
    except TaskEnded:
        ctx.stopped = True
        return None
    except RetryableError:
        return None


async def _safe_close(ctx: RunContext, rec: ChallengeRecord) -> None:
    if rec.container_status == "stopped" and not rec.active_since_reconcile:
        return
    rec.state = "CLOSING"
    with contextlib.suppress(TaskEnded, RetryableError, ChallengeGone, LevelLocked, StartFull):
        await ctx.client.close(rec.unique_code)  # 尽力而为；下一轮对账以平台真值为准
    rec.container_status = "stopped"
    rec.active_since_reconcile = False


async def _wait_available(ctx: RunContext, rec: ChallengeRecord, timeout_s: float) -> bool:
    """轮询等 container_status == available（platform-api.md §4.1）。

    自适应退避：见 AVAILABLE_POLL_* 常量说明 —— 固定快轮询会吃满令牌桶，
    把 submit/close 挤到队列后面。
    """
    deadline = time.monotonic() + timeout_s
    delay = AVAILABLE_POLL_START_S
    while time.monotonic() < deadline:
        rows = await _safe_list(ctx)
        if ctx.stopped:
            return False
        if rows is not None:
            row = next((r for r in rows if r["unique_code"] == rec.unique_code), None)
            if row is None:
                return False
            rec.container_status = row["container_status"]
            rec.container_addr = list(row.get("container_addr") or [])
            if rec.container_status == "available" and rec.container_addr:
                return True
            if rec.container_status == "stopped":
                return False
        await asyncio.sleep(delay)
        delay = min(AVAILABLE_POLL_MAX_S, delay * AVAILABLE_POLL_GROWTH)
    return False


def _merge_prior(old: dict | None, new: dict) -> dict:
    """双图跨轮合并：solver 只交原始观察，去重由编排器算（§2.5）。"""
    merged = {"assets": [], "explored": [], "open_leads": []}
    for key in merged:
        items = list((old or {}).get(key) or []) + list(new.get(key) or [])
        seen: list[str] = []
        for item in items:
            marker = str(sorted(item.items())) if isinstance(item, dict) else str(item)
            if marker not in seen:
                seen.append(marker)
                merged[key].append(item)
    return merged


def _remaining_score(registry: Registry) -> float:
    return sum(r.remaining_score for r in registry.records.values() if not r.is_completed)


# ---------------------------------------------------------------- pipeline ----
async def _pipeline(ctx: RunContext, code: str, slice_s: float) -> None:
    rec = ctx.registry.records[code]
    rec.attempts += 1
    rec.state = "STARTING"
    try:
        await ctx.client.start(code)
    except LevelLocked:
        ctx.registry.mark_locked(code)
        return  # LOCKED 不占额度、不需 close（§3.1）
    except StartFull:
        rec.state = "QUEUED"  # 并发满，下一轮对账后再试
        return
    except ChallengeGone:
        rec.state = "ABANDONED"
        return
    except TaskEnded:
        ctx.stopped = True
        return
    rec.active_since_reconcile = True
    ctx.journal.append("started", unique_code=code)

    rec.state = "POLLING"
    if not await _wait_available(ctx, rec, AVAILABLE_TIMEOUT_S):
        await _safe_close(ctx, rec)
        if rec.state == "CLOSING":
            rec.state = "CLOSED"
        rec.state = "QUEUED"
        return

    rec.state = "SOLVING"
    sops = sop_mod.select(ctx.workspace_root, rec.description or "")  # EvoSOP 检索（§4.3）
    ws = prepare_workspace(
        ctx.workspace_root,
        rec,
        attempt=rec.attempts,
        prior=ctx.priors.get(code),
        sops=sops,
        repair_note=ctx.repair_notes.get(code),
    )
    deadline_epoch = min(time.time() + slice_s, ctx.t0 + ctx.config.total_time_limit_s)
    # Staircase 流式切片（§2）：片内节拍早收割，不等整片跑完才提交
    outcome, early_new, early_completed = await streams.run_solver_with_stream(
        solver_cmd=ctx.solver_cmd,
        ws=ws,
        deadline_epoch=deadline_epoch,
        slice_s=slice_s,
        environ=ctx.env,
        client=ctx.client,
        journal=ctx.journal,
        unique_code=code,
        rec=rec,
    )
    ctx.journal.append(
        "solver_done",
        unique_code=code,
        exit_code=outcome.exit_code,
        timed_out=outcome.timed_out,
        duration_s=round(outcome.duration_s, 1),
    )

    rec.state = "HARVESTING"
    ctx.priors[code] = _merge_prior(ctx.priors.get(code), collect_prior(ws))
    if ctx.memory is not None:
        ctx.memory.record(code, ctx.priors[code])  # 攻击图记忆：单写者=编排器
    candidates = [f for f in harvest(ws) if f not in rec.submitted]

    rec.state = "SUBMITTING"
    report = await submit_candidates(ctx.client, rec, candidates)
    if report.task_ended:
        ctx.stopped = True
    if report.new_matches or early_new:
        # EVOSTER 锚定（§5）：上一轮修复动作命中 → 权重 +25%
        last = ctx.last_repair.get(code)
        if last:
            update_weights(ctx.repair_weights, last, success=True)
        # EvoSOP 蒸馏（§4）：本轮有命中 → 把可复用序列蒸馏入库（fail-closed）
        sop_mod.distill(ws, ctx.workspace_root)
        # 正确提交可能触发关卡解锁 → LOCKED 题重新入队（§5.1）
        for unlocked in ctx.registry.requeue_unlocked():
            ctx.journal.append("requeued_unlocked", unique_code=unlocked)

    # ---- 终局判定 ------------------------------------------------------------
    if rec.is_completed or report.completed or early_completed:
        rec.state = "SOLVED"
        await _safe_close(ctx, rec)
        rec.state = "CLOSED"
        ctx.repair_notes.pop(code, None)
        ctx.journal.append("solved", unique_code=code, matches=len(rec.matched_indices))
        return
    if ctx.stopped:
        await _safe_close(ctx, rec)
        rec.state = "CLOSED"
        return
    if report.new_matches or early_new:
        rec.state = "QUEUED"  # 多 flag 题继续（§4.6 不提前放弃）
        await _safe_close(ctx, rec)
        rec.state = "QUEUED"
        return
    if rec.attempts >= MAX_ATTEMPTS:
        rec.state = "ABANDONED"
        await _safe_close(ctx, rec)
        rec.state = "CLOSED"
        ctx.repair_notes.pop(code, None)
        ctx.journal.append("abandoned", unique_code=code)
        return
    # EVOSTER 在线修复（§5）：失败信号 → 有界动作；attempt 预算不动
    repair = choose_repair(
        rec,
        outcome,
        slice_s,
        total_remaining_s=ctx.t0 + ctx.config.total_time_limit_s - time.time(),
        slice_max_s=ctx.config.slice_max_s,
        weights=ctx.repair_weights,
    )
    ctx.last_repair[code] = repair.action
    ctx.journal.append("repair", unique_code=code, action=repair.action, note=repair.note)
    if repair.next_slice_s:
        rec.repair_widened = True  # widen 最多一次；切片只覆盖下一轮一次
        rec.next_slice_s = repair.next_slice_s
    if repair.note and repair.action == "recompile":
        ctx.repair_notes[code] = repair.note
    rec.state = "RETRY"
    await _safe_close(ctx, rec)
    rec.state = "QUEUED"  # 按修复计划重投（换策略/更大时间片）


# ------------------------------------------------------------------ run ----
async def run_agent(
    *,
    base_url: str,
    token: str,
    solver_cmd: list[str],
    config: RuntimeConfig | None = None,
    workspace: Path | None = None,
    journal_path: Path | None = None,
    env: dict[str, str] | None = None,
    qps: float = 3.0,
    poll_interval_s: float = POLL_INTERVAL_S,
) -> dict:
    """编排器主循环。返回计分摘要（awarded / cumulative_score）。"""
    config = config or RuntimeConfig(
        mode="managed",
        base_url=base_url.rstrip("/"),
        token=token,
        llm_base_url="",
        llm_api_key="",
        llm_model="deepseek-chat",
        total_time_limit_s=21600,
        reserve_seconds=600,
        concurrency_limit=3,
        api_max_qps=qps,
    )
    workspace = (workspace or Path("./workspaces")).resolve()
    journal = Journal(journal_path or (workspace / "journal.jsonl"))
    client = PlatformClient(base_url.rstrip("/"), token, qps=qps)
    # 攻击图记忆（harness-v2.md §6）：MEMORY_TDB=off 或 triviumdb 缺失 → 空操作降级
    memory = AttackMemory(
        workspace / "attack.tdb",
        enabled=os.environ.get("MEMORY_TDB", "on").strip().lower() != "off",
    )
    ctx = RunContext(
        config=config,
        client=client,
        registry=Registry(),
        journal=journal,
        workspace_root=workspace,
        solver_cmd=solver_cmd,
        env=env,
        t0=time.time(),
        memory=memory,
    )
    tasks: set[asyncio.Task] = set()
    reconciled = False
    try:
        while not ctx.stopped and time.time() - ctx.t0 < config.total_time_limit_s:
            rows = await _safe_list(ctx)
            if ctx.stopped or rows is None:
                break
            ctx.registry.sync(rows)
            if not reconciled:
                # 开局对账即关残留（round2 活锁教训）：上进程留下的 active 容器若
                # 占满平台全局 active 上限，start 持续 409 StartFull、eligible 又
                # 非空 → 2s 轮询空转到终局零解。此刻本进程必无在跑题，关掉立即
                # 释放槽位；被关的题下一轮 sync 见 stopped 即恢复 eligible。
                reconciled = True
                for rec in ctx.registry.records.values():
                    if rec.active_since_reconcile:
                        await _safe_close(ctx, rec)
                        rec.state = "QUEUED"
                        ctx.journal.append("reconcile_close", unique_code=rec.unique_code)
            plan = plan_phase(
                elapsed_s=time.time() - ctx.t0,
                total_s=config.total_time_limit_s,
                reserve_s=config.reserve_seconds,
                # 分母 = 真正还要解的题（可启动 + 在跑），在跑的也是预算消耗者
                n_eligible=len(eligible(ctx.registry.records)) + len(ctx.running),
                remaining_score=_remaining_score(ctx.registry),
                slice_min_s=config.slice_min_s,
                slice_max_s=config.slice_max_s,
                concurrency=config.concurrency_limit,
            )
            if plan.finalize:
                ctx.journal.append("finalize")
                break
            # 空闲即退出：没有在跑的题、也没有可启动的题（全部通关/LOCKED/放弃），
            # 继续轮询只会空烧预算。LOCKED 题只能靠自己的正确提交解锁，
            # 没有在跑的题时它永远等不到解锁信号。
            if not ctx.running and not eligible(ctx.registry.records):
                ctx.journal.append("idle_exit")
                break
            for rec in eligible(ctx.registry.records):
                if not can_start(
                    active_count=ctx.registry.active_count() + len(ctx.running),
                    concurrency_limit=config.concurrency_limit,
                ):
                    break
                if rec.unique_code in ctx.running:
                    continue
                rec.state = "QUEUED"
                # 分数加权（§4.3 修订）：高分多阶段题拿更长片；EVOSTER widen 的
                # 覆盖值（已在上一轮按加权片算好）优先，用一次即清。
                attempt_slice = rec.next_slice_s or slice_for(
                    rec,
                    plan.slice_s,
                    slice_min_s=config.slice_min_s,
                    slice_max_s=plan.slice_cap_s,
                )
                rec.next_slice_s = None
                task = asyncio.create_task(_pipeline(ctx, rec.unique_code, attempt_slice))
                tasks.add(task)
                ctx.running.add(rec.unique_code)
                task.add_done_callback(lambda t, c=rec.unique_code: ctx.running.discard(c))
            await asyncio.sleep(poll_interval_s)
        # 收尾保护（§4.3）：等在跑的 pipeline 结束，再强制 close 一切活跃容器
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for rec in ctx.registry.records.values():
            if rec.active_since_reconcile:
                await _safe_close(ctx, rec)
                rec.state = "CLOSED"
        rows = await _safe_list(ctx)
        summary = score_summary(rows or [])
        summary["per_challenge"] = {
            code: {"state": rec.state, "attempts": rec.attempts}
            for code, rec in sorted(ctx.registry.records.items())
        }
        summary["memory"] = memory.stats()  # 攻击图记忆：节点数/边数（降级时为 enabled: False）
        journal.append("run_end", **{k: v for k, v in summary.items() if k != "per_challenge"})
        return summary
    finally:
        for task in tasks:
            task.cancel()
        memory.close()
        journal.close()


def main() -> int:
    config = resolve_config()
    # solver 在题目工作区目录下被启动（cwd ≠ 仓库根），`-m solver.main` 不可导入 ——
    # 用仓库内绝对路径直接执行脚本（solver/main.py 自带 .env 定位，不依赖 cwd）。
    solver_main = Path(__file__).resolve().parents[3] / "solver" / "main.py"
    summary = asyncio.run(
        run_agent(
            base_url=config.base_url,
            token=config.token,
            solver_cmd=config.solver_cmd or [sys.executable, str(solver_main)],
            config=config,
        )
    )
    print(f"[orchestrator] done: {summary}")
    return 0
