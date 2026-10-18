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
import contextlib
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
# 每次编排器进程启动 = 新会话：复用同名 attempt 目录时据此清掉上一进程的产物。
_RUN_ID = f"{os.getpid()}-{int(time.time())}"


@dataclass(slots=True)
class SolverOutcome:
    exit_code: int | None
    timed_out: bool
    duration_s: float
    stdout_path: Path
    stderr_path: Path
    stalled: bool = False  # 停滞看门狗：进程活着但长时间无输出/无新产物


# ---------------------------------------------------------------- workspace ----
def workspace_root(base: Path) -> Path:
    return base / "workspaces"


def prepare_workspace(
    base: Path,
    rec: ChallengeRecord,
    *,
    attempt: int,
    prior: dict | None = None,
    sops: list[str] | None = None,
    repair_note: str | None = None,
) -> Path:
    """创建该题第 attempt 轮的 workspace 并写 task.json（§2.1 形状 + harness-v2 §3/§4/§5 注入）。"""
    root = workspace_root(base)
    ws = root / f"{rec.unique_code}_attempt{attempt}"
    ws.mkdir(parents=True, exist_ok=True)
    _reset_if_other_instance(ws, rec.container_addr)
    merged = dict(prior or {})
    previous = _previous_attempt(root, rec.unique_code, ws, rec.container_addr)
    if previous is not None:
        # 上一投被关闭/超时后，文件仍在磁盘：把 prior/history 合并，并把
        # 可复用文件路径写进提示，避免下一投重新侦察/重新写脚本。
        merged = _merge_prior(merged, collect_prior(previous))
        merged["previous_workspace"] = str(previous.resolve())
        merged["carried_files"] = _carried_files(previous)
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
        "attempt": attempt,
        "run_id": _RUN_ID,
        "prior": merged
        or {
            "assets": [],
            "explored": [],
            "open_leads": [],
            "credentials": [],
            "findings": [],
        },
        # harness-v2 注入位（solver 侧缺失时不使用，向后兼容）：
        "sops": list(sops or []),  # EvoSOP：库里检索出的已蒸馏 SOP 正文
        "repair": {"note": repair_note} if repair_note else {},  # EVOSTER 修复指令
    }
    (ws / "task.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    (ws / "out" / "flags").mkdir(parents=True, exist_ok=True)
    return ws


def _dedupe(values: list) -> list:
    out: list = []
    seen: set[str] = set()
    for value in values:
        key = (
            json.dumps(value, sort_keys=True, ensure_ascii=False)
            if isinstance(value, (dict, list))
            else str(value)
        )
        if not key.strip() or key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _instance_sig(targets: list[str] | None) -> str:
    return "|".join(sorted(str(t) for t in (targets or [])))


def _reset_if_other_instance(ws: Path, targets: list[str] | None) -> None:
    """同一 attempt 目录被新进程/新 target 复用时，清掉上一轮的 out/ 与日志。

    平台每次 start 可能给同一 unique_code 分配不同的 target IP（实测 a-03:
    .48 -> .50）。旧 out/flags、submitted.txt、prior.json 里的 IP 在新一局
    全部失效；继续沿用会让 solver 去打别的题的目标，并把旧 flag 反复投递。
    """
    sig = _instance_sig(targets)
    if not sig:
        return
    stamp = f"{_RUN_ID}|{sig}"
    marker = ws / ".instance"
    old = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    same = old == stamp
    if not old:
        try:
            stored = json.loads((ws / "task.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = None
        same = (
            bool(stored)
            and stored.get("run_id") == _RUN_ID
            and _instance_sig(stored.get("targets")) == sig
        )
    if same:
        marker.write_text(stamp, encoding="utf-8")
        return
    out = ws / "out"
    if out.is_dir() and any(out.iterdir()):
        stale = ws / f"out.prev-{int(time.time())}"
        with contextlib.suppress(OSError):
            out.rename(stale)
    for name in ("stdout.log", "stderr.log"):
        with contextlib.suppress(OSError):
            (ws / name).unlink()
    for extra in ws.glob("stderr.log.*.log"):
        with contextlib.suppress(OSError):
            extra.unlink()
    marker.write_text(stamp, encoding="utf-8")


def _merge_prior(base: dict, extra: dict) -> dict:
    merged = dict(base or {})
    for key in ("assets", "explored", "open_leads", "credentials", "findings"):
        merged[key] = _dedupe(
            list((base or {}).get(key) or []) + list((extra or {}).get(key) or [])
        )
    histories = [
        str((base or {}).get("history") or "").strip(),
        str((extra or {}).get("history") or "").strip(),
    ]
    history = "\n".join(part for part in histories if part)
    if history:
        merged["history"] = history[-4000:]
    stage_state = (extra or {}).get("stage_state") or (base or {}).get("stage_state")
    if isinstance(stage_state, dict) and stage_state:
        merged["stage_state"] = stage_state
    new_obs = (extra or {}).get("observer")
    if isinstance(new_obs, dict) and new_obs:
        old_obs = (base or {}).get("observer")
        old_rev = int(old_obs.get("revision") or 0) if isinstance(old_obs, dict) else -1
        if int(new_obs.get("revision") or 0) >= old_rev:
            merged["observer"] = new_obs
    return merged


def _previous_attempt(
    root: Path, code: str, current: Path, targets: list[str] | None = None
) -> Path | None:
    """上一 attempt 目录；只认**同一 target 实例**（IP 变了就是新一局）。"""
    sig = _instance_sig(targets)
    best: Path | None = None
    best_mtime = -1.0
    for path in root.glob(f"{code}_attempt*"):
        if path == current:
            continue
        prior_file = path / "out" / "prior.json"
        if not prior_file.is_file():
            continue
        if sig:
            try:
                task = json.loads((path / "task.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                task = None
            if task is not None:
                if task.get("run_id") != _RUN_ID:
                    continue  # 上一进程/上一局：IP 与凭据都可能失效
                if _instance_sig(task.get("targets")) != sig:
                    continue
        with contextlib.suppress(OSError):
            mtime = prior_file.stat().st_mtime
            if mtime > best_mtime:
                best, best_mtime = path, mtime
    return best


def _carried_files(previous: Path, limit: int = 20) -> list[str]:
    """上一 attempt 里可复用的小文件；大输出只给目录，让模型自己 grep。"""
    files: list[str] = []
    for rel in ("notes.md", "out/prior.json", "out/findings.txt", "out/harness_state.txt"):
        path = previous / rel
        if path.is_file():
            files.append(str(path.resolve()))
    for pattern in ("*.py", "*.sh", "*.txt", "*.json"):
        for path in sorted(previous.glob(pattern)):
            with contextlib.suppress(OSError):
                if path.is_file() and path.stat().st_size <= 1_000_000:
                    files.append(str(path.resolve()))
    outputs = previous / "out" / "tool_outputs"
    if outputs.is_dir():
        files.append(str(outputs.resolve()) + "/ (grep -r for full outputs)")
    return list(dict.fromkeys(files))[:limit]


def collect_prior(ws: Path) -> dict:
    """读 out/prior.json；缺失或畸形**不是致命错误**（§2.2 末段），降级为空。"""
    prior: dict = {
        "assets": [],
        "explored": [],
        "open_leads": [],
        "credentials": [],
        "findings": [],
        "history": "",
    }
    path = ws / "out" / "prior.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict):
        for key in ("assets", "explored", "open_leads", "credentials", "findings"):
            value = data.get(key)
            if isinstance(value, list):
                prior[key] = value
    # 跨 attempt 记忆：solver 每轮落盘的命令/结果摘要，下一投不再从零开始
    with contextlib.suppress(OSError):
        prior["history"] = (ws / "out" / "harness_state.txt").read_text(encoding="utf-8")[:4000]
    # Observer-lite：卡住时的一次反思，跨切片/跨 attempt 注入下一轮 system
    try:
        obs = json.loads((ws / "out" / "observer.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        obs = None
    if isinstance(obs, dict):
        prior["observer"] = {
            key: obs.get(key)
            for key in ("revision", "hypothesis", "lock", "confirmed", "angles", "tension")
            if obs.get(key)
        }
    try:
        stage_state = json.loads((ws / "out" / "stage_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stage_state = None
    if isinstance(stage_state, dict):
        prior["stage_state"] = stage_state
    return prior


# ---------------------------------------------------------------- spawn ----
def _kill_shell(ws: Path) -> None:
    pid_path = ws / "out" / "shell.pid"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        pid_path.unlink()


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


def _kill_now(proc: subprocess.Popen) -> None:
    """立即 SIGKILL 整个进程组并回收（取消路径不可优雅等待）。"""
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    else:
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=3)


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
    finally:
        # 超时 / 任务取消（pipeline 异常）都必须杀掉进程组：否则 solver 与它
        # 拉起的 nmap/curl 会变成孤儿继续跑（run-35236 c-06 实测）。
        _kill_shell(ws)
        if proc.poll() is None:
            _kill_now(proc)
        stdout_f.close()
        stderr_f.close()
    duration = time.monotonic() - started
    exit_code = 124 if timed_out else proc.returncode
    return SolverOutcome(
        exit_code=exit_code,
        timed_out=timed_out,
        duration_s=duration,
        stdout_path=ws / "stdout.log",
        stderr_path=ws / "stderr.log",
    )
