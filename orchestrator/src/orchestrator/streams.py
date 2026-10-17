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

from .harvester import harvest, submit_candidates
from .supervisor import SolverOutcome, run_solver

STREAM_INTERVAL_S = 45.0  # 中途收割节拍（§2.2）


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
) -> tuple[SolverOutcome, int, bool]:
    """跑一整片 solver，片内按节拍早收割。

    返回 (outcome, 早交命中数, 早交已完成)。片尾仍会跑原收割/提交路径，
    去重由 rec.submitted 保证两边不重复计。
    """
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
    try:
        while True:
            done, _pending = await asyncio.wait({solver_task}, timeout=interval_s)
            if done:
                outcome = solver_task.result()
                break
            candidates = [f for f in harvest(ws) if f not in rec.submitted]
            if not candidates:
                continue
            report = await submit_candidates(client, rec, candidates)
            early_completed = early_completed or report.completed
            if report.new_matches:
                early_new += report.new_matches
                journal.append(
                    "early_submit", unique_code=unique_code, new_matches=report.new_matches
                )
    except BaseException:
        # 提交异常（WAF/网络）不能让 solver 成为孤儿：取消并等待其清理进程组。
        solver_task.cancel()
        with contextlib.suppress(BaseException):
            await solver_task
        raise
    return outcome, early_new, early_completed
