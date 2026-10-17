"""Staircase 节拍收割 —— solver 在跑时提前发现并提交候选（harness-v2.md §2）。

原编排器等 run_solver 整片跑完才进 HARVESTING：flag 在第 2 分钟产出、
时间片 20 分钟时，提交要拖到片尾（TTFT 语义的最大延迟源）。本模块把
[SOLVING→HARVESTING→SUBMITTING] 的串行改成阶梯：以 STREAM_INTERVAL_S
节拍扫 workspace，新候选立即提交；completed 信号也尽早带出。

红线不变（§2.3）：
  - 不杀在跑的 solver（早交后仍等自然退出，收尾走原状态机）；
  - 去重仍靠 rec.submitted（早交与片尾收割同一真值）；
  - 计分经济学不动（correct_flag_count 是平台真值，本地早交不修改）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

from .harvester import harvest, submit_candidates
from .supervisor import SolverOutcome, run_solver

STREAM_INTERVAL_S = 45.0  # 中途收割节拍（§2.2）
# 停滞看门狗：solver 进程活着但 stdout 与 out/ 产物长时间不变 = LLM 流或工具挂死。
# 单条命令上限 120s，240s 静默足以排除正常长命令；0 = 关闭。
DEFAULT_STALL_S = 240.0


def stall_seconds() -> float:
    raw = os.environ.get("STALL_S")
    if raw is None or raw.strip() == "":
        return DEFAULT_STALL_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_STALL_S


def _newest_mtime(path: Path) -> float:
    newest = 0.0
    try:
        newest = path.stat().st_mtime
    except OSError:
        return 0.0
    with contextlib.suppress(OSError):
        for child in path.rglob("*"):
            with contextlib.suppress(OSError):
                newest = max(newest, child.stat().st_mtime)
    return newest


def activity_signature(ws: Path) -> tuple[int, float, float, int]:
    """solver 活动签名：stdout 大小/时间 + out/ 最新产物时间 + 候选数。"""
    stdout = ws / "stdout.log"
    size = 0
    mtime = 0.0
    with contextlib.suppress(OSError):
        stat = stdout.stat()
        size, mtime = stat.st_size, stat.st_mtime
    out = ws / "out"
    flags = 0
    with contextlib.suppress(OSError):
        flags = len(list((out / "flags").glob("*.txt")))
    return (size, mtime, _newest_mtime(out), flags)


async def run_solver_with_stream(
    *,
    solver_cmd: list[str],
    ws,
    deadline_epoch: float,
    slice_s: float,
    environ: dict[str, str] | None,
    client,
    journal,
    unique_code: str,
    rec,
    interval_s: float = STREAM_INTERVAL_S,
    stall_s: float | None = None,
) -> tuple[SolverOutcome, int, bool]:
    """跑一整片 solver，片内按节拍早收割。

    返回 (outcome, 早交命中数, 早交已完成)。片尾仍会跑原收割/提交路径，
    去重由 rec.submitted 保证两边不重复计。

    停滞看门狗（本函数新增）：每 interval_s 检查 activity_signature；连续
    stall_s 无变化 → 取消 solver（run_solver 的 finally 会 killpg）并返回
    stalled=True，让 repair 走 recompile，而不是占着槽等整片到点。
    """
    stall_after = stall_seconds() if stall_s is None else max(0.0, stall_s)
    solver_task = asyncio.create_task(
        run_solver(
            solver_cmd=solver_cmd,
            ws=ws,
            deadline_epoch=deadline_epoch,
            slice_s=slice_s,
            environ=environ,
        )
    )
    early_new, early_completed = 0, False
    started = time.monotonic()
    last_sig = activity_signature(ws)
    last_change = started
    try:
        while True:
            done, _pending = await asyncio.wait({solver_task}, timeout=interval_s)
            if done:
                outcome = solver_task.result()
                break
            sig = activity_signature(ws)
            now = time.monotonic()
            if sig != last_sig:
                last_sig = sig
                last_change = now
            elif stall_after > 0 and now - last_change >= stall_after:
                solver_task.cancel()
                with contextlib.suppress(BaseException):
                    await solver_task
                journal.append(
                    "solver_stalled",
                    unique_code=unique_code,
                    idle_s=round(now - last_change, 1),
                )
                return (
                    SolverOutcome(
                        exit_code=124,
                        timed_out=False,
                        duration_s=time.monotonic() - started,
                        stdout_path=ws / "stdout.log",
                        stderr_path=ws / "stderr.log",
                        stalled=True,
                    ),
                    early_new,
                    early_completed,
                )
            candidates = [f for f in harvest(ws) if f not in rec.submitted]
            if not candidates:
                continue
            report = await submit_candidates(client, rec, candidates)
            if report.new_matches or report.rejected or report.duplicates:
                journal.append(
                    "submit_result",
                    unique_code=unique_code,
                    matched=report.new_matches,
                    rejected=report.rejected,
                    duplicates=report.duplicates,
                    source="stream",
                )
            early_completed = early_completed or report.completed
            if report.new_matches:
                early_new += report.new_matches
                journal.append(
                    "early_submit", unique_code=unique_code, new_matches=report.new_matches
                )
                # 单 flag 题一旦命中就立即停 solver，释放槽；多 flag 继续找剩余。
                if report.completed or rec.flag_count == 1:
                    solver_task.cancel()
                    with contextlib.suppress(BaseException):
                        await solver_task
                    journal.append(
                        "early_stop",
                        unique_code=unique_code,
                        elapsed_s=round(time.monotonic() - started, 1),
                    )
                    return (
                        SolverOutcome(
                            exit_code=0,
                            timed_out=False,
                            duration_s=time.monotonic() - started,
                            stdout_path=ws / "stdout.log",
                            stderr_path=ws / "stderr.log",
                        ),
                        early_new,
                        True,
                    )
    except BaseException:
        # 提交异常（WAF/网络）不能让 solver 成为孤儿：取消并等待其清理进程组。
        solver_task.cancel()
        with contextlib.suppress(BaseException):
            await solver_task
        raise
    return outcome, early_new, early_completed
