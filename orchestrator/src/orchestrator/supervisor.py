"""Solver Supervisor —— 每题一个子进程（architecture.md §3 / §5.2）。

职责：workspace 创建与清理、task.json 生成、白名单环境 spawn、
硬超时 SIGTERM → 宽限 → SIGKILL 打**整个进程组**（防 nmap/sqlmap 孤儿）、
stdout/stderr 全量落盘、exit code 收集。

契约（§2.1/§2.2）：
  - 输入：SOLVER_TASK 指向 task.json；LLM_*/SOLVER_*/代理变量走白名单；
  - 输出：out/flags/*.txt（候选，一文件一个）+ out/prior.json + out/result.json；
  - 编排器**不信任 result.json**，收割交给 Flag Harvester；
  - exit 124 = 超时被杀；被杀前已产出的候选仍会被收割（§5.2）。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import build_solver_env
from .registry import ChallengeRecord

GRACE_S = 10.0  # SIGTERM 后等待自行退出的宽限


@dataclass(slots=True)
class SolverOutcome:
    exit_code: int | None
    timed_out: bool
    duration_s: float
    stdout_path: Path
    stderr_path: Path


# ---------------------------------------------------------------- workspace ----
def workspace_root(base: Path) -> Path:
    return base / "workspaces"


def prepare_workspace(
    base: Path,
    rec: ChallengeRecord,
    *,
    attempt: int,
    prior: dict | None = None,
    hint: str | None = None,
    sops: list[str] | None = None,
    repair_note: str | None = None,
) -> Path:
    """创建该题第 attempt 轮的 workspace 并写 task.json（§2.1 形状 + harness-v2 §3/§4/§5 注入）。"""
    ws = workspace_root(base) / f"{rec.unique_code}_attempt{attempt}"
    ws.mkdir(parents=True, exist_ok=True)
    task = {
        "unique_code": rec.unique_code,
        "description": rec.description,
        "difficulty": rec.difficulty,
        "level": rec.level,
        "total_score": rec.total_score,
        "flag_count": rec.flag_count,
        "correct_flag_count": rec.correct_flag_count,
        "targets": list(rec.container_addr),
        "flag_pattern": None,  # 平台不提供；从 description 推断或靠通用正则兜底
        "hint": hint,
        "attempt": attempt,
        "prior": prior or {"assets": [], "explored": [], "open_leads": []},
        # harness-v2 注入位（solver 侧缺失时不使用，向后兼容）：
        "sops": list(sops or []),  # EvoSOP：库里检索出的已蒸馏 SOP 正文
        "repair": {"note": repair_note} if repair_note else {},  # EVOSTER 修复指令
    }
    (ws / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    (ws / "out" / "flags").mkdir(parents=True, exist_ok=True)
    return ws


def collect_prior(ws: Path) -> dict:
    """读 out/prior.json；缺失或畸形**不是致命错误**（§2.2 末段），降级为空。"""
    path = ws / "out" / "prior.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError, ValueError:
        return {"assets": [], "explored": [], "open_leads": []}
    if not isinstance(data, dict):
        return {"assets": [], "explored": [], "open_leads": []}
    out = {"assets": [], "explored": [], "open_leads": []}
    for key in out:
        value = data.get(key)
        if isinstance(value, list):
            out[key] = value
    return out


# ---------------------------------------------------------------- spawn ----
def _kill_tree(proc: subprocess.Popen) -> None:
    """杀整个进程组（POSIX）；Windows 退化杀单进程。"""
    if os.name == "posix":
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                return
            try:
                proc.wait(timeout=GRACE_S)
                return
            except subprocess.TimeoutExpired:
                continue
    else:
        proc.kill()
        proc.wait(timeout=GRACE_S)


async def run_solver(
    *,
    solver_cmd: list[str],
    ws: Path,
    deadline_epoch: float,
    slice_s: float,
    environ: dict[str, str] | None = None,
) -> SolverOutcome:
    """spawn solver 并施加时间片硬超时。stdout/stderr 全量落盘。"""
    task_path = ws / "task.json"
    env = build_solver_env(
        environ,
        extra={
            "SOLVER_TASK": str(task_path),
            "SOLVER_DEADLINE_EPOCH": str(int(deadline_epoch)),
            "SOLVER_BUDGET_HINT": str(int(slice_s)),
        },
    )
    cmd = solver_cmd or [os.environ.get("SOLVER_PYTHON", "python"), "-m", "solver.main"]

    started = time.monotonic()
    stdout_f = open(ws / "stdout.log", "wb")  # noqa: SIM115 - 生命周期与子进程一致
    stderr_f = open(ws / "stderr.log", "wb")  # noqa: SIM115
    try:
        proc = subprocess.Popen(  # noqa: S603 - solver_cmd 是自有配置
            cmd,
            cwd=str(ws),
            stdout=stdout_f,
            stderr=stderr_f,
            env=env,
            start_new_session=(os.name == "posix"),
        )
    except OSError:
        stdout_f.close()
        stderr_f.close()
        return SolverOutcome(
            exit_code=127,
            timed_out=False,
            duration_s=0.0,
            stdout_path=ws / "stdout.log",
            stderr_path=ws / "stderr.log",
        )

    timed_out = False
    try:
        await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=slice_s)
    except TimeoutError:
        timed_out = True
        await asyncio.to_thread(_kill_tree, proc)

    duration = time.monotonic() - started
    stdout_f.close()
    stderr_f.close()
    exit_code = 124 if timed_out else proc.returncode
    return SolverOutcome(
        exit_code=exit_code,
        timed_out=timed_out,
        duration_s=duration,
        stdout_path=ws / "stdout.log",
        stderr_path=ws / "stderr.log",
    )
