"""编排器主循环（architecture.md §3.1 状态机 / §4.3 两阶段预算 / §3.2 崩溃恢复）。

CLI 入口：`python -m orchestrator`。
对 mock 可全链路真实测试：list → start → poll available → solver → harvest →
submit → close，含 LOCKED 重入队、hint 决策、收尾保护、崩溃恢复对账。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .api import (
    ChallengeGone,
    HintCompleted,
    LevelLocked,
    PlatformClient,
    RetryableError,
    StartFull,
    TaskEnded,
)
from .config import RuntimeConfig, resolve_config
from .harvester import harvest, submit_candidates
from .journal import Journal, score_summary
from .registry import ChallengeRecord, Registry
from .scheduler import can_start, eligible, plan_phase
from .supervisor import collect_prior, prepare_workspace, run_solver

POLL_INTERVAL_S = 2.0
AVAILABLE_TIMEOUT_S = 120.0
MAX_ATTEMPTS = 2  # 非 hint 路径的普通重试上限


@dataclass(slots=True)
class RunContext:
    config: RuntimeConfig
    client: PlatformClient
    registry: Registry
    journal: Journal
    workspace_root: Path
    solver_cmd: list[str]
    hints: dict[str, str] = field(default_factory=dict)  # code → hint 文本（下轮回灌）
    priors: dict[str, dict] = field(default_factory=dict)  # code → 双图状态（跨轮合并）
    running: set[str] = field(default_factory=set)
    stopped: bool = False
    t0: float = 0.0
    env: dict[str, str] | None = None  # 传给 solver 的基础环境（测试注入用）


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
    """轮询等 container_status == available（platform-api.md §4.1）。"""
    deadline = time.monotonic() + timeout_s
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
        await asyncio.sleep(0.2)
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
    ws = prepare_workspace(
        ctx.workspace_root,
        rec,
        attempt=rec.attempts,
        prior=ctx.priors.get(code),
        hint=ctx.hints.get(code),
    )
    deadline_epoch = min(time.time() + slice_s, ctx.t0 + ctx.config.total_time_limit_s)
    outcome = await run_solver(
        solver_cmd=ctx.solver_cmd,
        ws=ws,
        deadline_epoch=deadline_epoch,
        slice_s=slice_s,
        environ=ctx.env,
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
    candidates = [f for f in harvest(ws) if f not in rec.submitted]

    rec.state = "SUBMITTING"
    report = await submit_candidates(ctx.client, rec, candidates)
    if report.task_ended:
        ctx.stopped = True
    if report.new_matches:
        # 正确提交可能触发关卡解锁 → LOCKED 题重新入队（§5.1）
        for unlocked in ctx.registry.requeue_unlocked():
            ctx.journal.append("requeued_unlocked", unique_code=unlocked)

    # ---- 终局判定 ------------------------------------------------------------
    if rec.is_completed or report.completed:
        rec.state = "SOLVED"
        await _safe_close(ctx, rec)
        rec.state = "CLOSED"
        ctx.journal.append("solved", unique_code=code, matches=len(rec.matched_indices))
        return
    if ctx.stopped:
        await _safe_close(ctx, rec)
        rec.state = "CLOSED"
        return
    if report.new_matches:
        rec.state = "QUEUED"  # 多 flag 题继续（§4.6 不提前放弃）
        await _safe_close(ctx, rec)
        rec.state = "QUEUED"
        return
    if rec.correct_flag_count == 0 and rec.hints_taken == 0 and not rec.is_completed:
        # Hint Broker 触发件（§4.4）：完整时间片 + 零 flag + 未取过 + 预算够再投一轮
        remaining = ctx.t0 + ctx.config.total_time_limit_s - time.time()
        if remaining > 2 * slice_s:
            hint: str | None = None
            try:
                hint = await ctx.client.hint(code)
            except HintCompleted:
                rec.is_completed = True  # 题目级 409：该题已通关（§4.5）
            except RetryableError, ChallengeGone, StartFull, LevelLocked:
                hint = None
            except TaskEnded:
                ctx.stopped = True
            if hint:
                ctx.hints[code] = hint
                rec.hints_taken += 1
                ctx.journal.append_hint(code, attempt=rec.attempts)
                rec.state = "HINT_RETRY"
                await _safe_close(ctx, rec)
                rec.state = "QUEUED"  # 带 hint 重新入队
                return
    if rec.attempts >= MAX_ATTEMPTS:
        rec.state = "ABANDONED"
        await _safe_close(ctx, rec)
        rec.state = "CLOSED"
        ctx.journal.append("abandoned", unique_code=code)
        return
    rec.state = "RETRY"
    await _safe_close(ctx, rec)
    rec.state = "QUEUED"  # 重新排队换策略再投


# ------------------------------------------------------------------ run ----
async def run_agent(
    *,
    base_url: str,
    token: str,
    solver_cmd: list[str],
    config: RuntimeConfig | None = None,
    workspace: Path | None = None,
    journal_path: Path | None = None,
    initial_hints: dict[str, int] | None = None,
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
        hint_cost_threshold=0.1,
    )
    workspace = (workspace or Path("./workspaces")).resolve()
    journal = Journal(journal_path or (workspace / "journal.jsonl"))
    client = PlatformClient(base_url.rstrip("/"), token, qps=qps)
    ctx = RunContext(
        config=config,
        client=client,
        registry=Registry(),
        journal=journal,
        workspace_root=workspace,
        solver_cmd=solver_cmd,
        env=env,
        t0=time.time(),
    )
    # 崩溃恢复：Journal 里「已取过 hint」的题恢复计数（平台读不回来，§4.4）
    replayed = journal.replay()
    recovered = initial_hints or {
        code: info["hints_taken"] for code, info in replayed.items() if info.get("hints_taken")
    }

    tasks: set[asyncio.Task] = set()
    try:
        while not ctx.stopped and time.time() - ctx.t0 < config.total_time_limit_s:
            rows = await _safe_list(ctx)
            if ctx.stopped or rows is None:
                break
            ctx.registry.sync(rows)
            for code, count in recovered.items():
                if code in ctx.registry.records:
                    ctx.registry.records[code].hints_taken = count
            plan = plan_phase(
                elapsed_s=time.time() - ctx.t0,
                total_s=config.total_time_limit_s,
                reserve_s=config.reserve_seconds,
                n_eligible=len(eligible(ctx.registry.records)),
                remaining_score=_remaining_score(ctx.registry),
                slice_min_s=config.slice_min_s,
                slice_max_s=config.slice_max_s,
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
                task = asyncio.create_task(_pipeline(ctx, rec.unique_code, plan.slice_s))
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
            code: {"state": rec.state, "attempts": rec.attempts, "hints": rec.hints_taken}
            for code, rec in sorted(ctx.registry.records.items())
        }
        journal.append("run_end", **{k: v for k, v in summary.items() if k != "per_challenge"})
        return summary
    finally:
        for task in tasks:
            task.cancel()
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
