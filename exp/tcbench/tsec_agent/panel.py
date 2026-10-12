#!/usr/bin/env python3
"""本地测试观察面板：实时展示 bench 跑分状态。

数据源（只读，不碰 agent 运行时）：
  1. bench 持久化记录 runs/<TOKEN>/（meta.json / tasks.jsonl / events.jsonl / conversation.jsonl）
     - 题目生命周期（启动/关闭/耗时/结果/flags）
     - 每题最近一次模型输出（thinking/output）、进行中的 shell 调用
       （assistant 已发出 tool_call 但尚无对应 tool 结果 = 正在运行）
  2. 平台 API（token = 目录名）：全局题数/通关数/flag 数/总分；离线时回退到本地记录

用法：
  python3 panel.py                     # 自动找最新的 runs/<TOKEN> 目录
  python3 panel.py --dir /path/to/runs/<TOKEN>
  python3 panel.py --interval 3 --once  # 3 秒刷新；--once 只渲染一帧（测试用）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

REFRESH_DEFAULT = 5


# ---------------- 数据读取 ----------------

def _read_jsonl_tail(path: str, n: int = 40) -> list[dict]:
    """读取 jsonl 末尾 n 条（容忍写入中的半行）。"""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-n:]


def _read_jsonl_all(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def load_tasks(runs_dir: str) -> dict[str, dict]:
    """tasks.jsonl → {code: {phase, started_at, closed_at, outcome, flags, duration, attempt, target}}"""
    tasks: dict[str, dict] = {}
    for r in _read_jsonl_all(os.path.join(runs_dir, "tasks.jsonl")):
        code = r.get("code", "?")
        t = tasks.setdefault(code, {"code": code})
        if r.get("phase") == "task_start":
            t.update(started_at=r.get("started_at"), target=r.get("target"),
                     attempt=r.get("attempt"), running=True)
        elif r.get("phase") == "task_close":
            t.update(closed_at=r.get("closed_at"), outcome=r.get("outcome"),
                     flags=r.get("flags"), duration=r.get("duration_sec"),
                     summary=(r.get("summary") or "")[:400], running=False)
    return tasks


def load_meta(runs_dir: str) -> dict:
    try:
        with open(os.path.join(runs_dir, "meta.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def load_recent_events(runs_dir: str, n: int = 6) -> list[dict]:
    return _read_jsonl_tail(os.path.join(runs_dir, "events.jsonl"), n)[::-1]


def task_live_view(runs_dir: str, code: str) -> dict:
    """单题对话的实时视角：最近模型输出 + 进行中/最近的 shell 调用。"""
    conv_path = os.path.join(runs_dir, "tasks", code, "conversation.jsonl")
    msgs = _read_jsonl_tail(conv_path, 60)
    view = {"last_output": "", "last_thinking": "", "running_cmds": [], "last_cmd": ""}
    pending: list[str] = []          # 已发出、未回结果的 tool_call
    finished: list[str] = []         # 最近完成的命令
    for r in msgs:
        kind = r.get("kind")
        if kind == "assistant":
            if r.get("output"):
                view["last_output"] = r["output"]
            if r.get("thinking"):
                view["last_thinking"] = r["thinking"]
            for tc in (r.get("tool_calls") or []):
                try:
                    args = json.loads(tc.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                action = args.get("action", "shell")
                if args.get("command"):
                    desc = f"{action}: {args['command']}"
                else:  # wait/status/kill 等无 command 的动作
                    desc = f"{action} " + " ".join(
                        f"{k}={v}" for k, v in args.items() if k != "action")
                pending.append(desc[:110])
        elif kind == "tool":
            tcid = r.get("tool_call_id")
            cmd = r.get("arguments", {}).get("command", "") if isinstance(r.get("arguments"), dict) else ""
            finished.append(str(cmd)[:110])
            # 结果回来了，移除对应 pending（简单法：清掉第一条同类）
            for i, p in enumerate(pending):
                if str(r.get("tool", "")) in p or (cmd and cmd[:40] in p):
                    pending.pop(i)
                    break
            else:
                if pending:
                    pending.pop(0)
    view["running_cmds"] = pending[:4]
    view["last_cmd"] = finished[-1] if finished else ""
    return view


def fetch_platform(runs_dir: str, base_url: str, timeout: float = 8.0) -> dict | None:
    """token = 目录名。返回 {total, completed, flags, max_flags, score} 或 None（离线）。"""
    token = os.path.basename(runs_dir.rstrip("/"))
    if not token or not base_url:
        return None
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/openapi/v1/challenges",
        headers={"BENCHMARK_TOKEN": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            items = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(items, list):
        return None
    completed = [c for c in items if c.get("is_completed")]
    return {
        "total": len(items),
        "completed": len(completed),
        "flags": sum(c.get("correct_flag_count", 0) for c in items),
        "max_flags": sum(c.get("flag_count", 0) for c in items),
        "score": sum(c.get("total_score", 0) for c in completed),
    }


def env_base_url() -> str:
    if os.environ.get("BENCHMARK_BASE_URL"):
        return os.environ["BENCHMARK_BASE_URL"]
    # 向上找项目根 .env
    d = os.path.abspath(os.getcwd())
    for _ in range(5):
        p = os.path.join(d, ".env")
        if os.path.isfile(p):
            for line in open(p, encoding="utf-8"):
                if line.startswith("BENCHMARK_BASE_URL="):
                    return line.split("=", 1)[1].strip()
        d = os.path.dirname(d)
    return "https://tsecbench.zc.tencent.com"


def find_latest_runs() -> str | None:
    """自动发现最新 runs/<TOKEN>：当前目录、../docker/runs、项目根。"""
    candidates = []
    for base in ("runs", "../docker/runs", "../../docker/runs",
                 "/home/xsf/Projects/tcbench/docker/runs"):
        if os.path.isdir(base):
            for name in os.listdir(base):
                p = os.path.join(base, name)
                if os.path.isdir(p):
                    candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda p: os.path.getmtime(p))


# ---------------- 渲染 ----------------

def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _one_line(text: str, width: int) -> str:
    text = (text or "").replace("\n", " ").replace("\r", " ").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def render(runs_dir: str, base_url: str) -> str:
    now = time.strftime("%H:%M:%S")
    meta = load_meta(runs_dir)
    tasks = load_tasks(runs_dir)
    events = load_recent_events(runs_dir)
    plat = fetch_platform(runs_dir, base_url)

    run_info = (meta.get("runs") or [{}])[-1]
    token_hint = os.path.basename(runs_dir.rstrip("/"))[:8]
    lines: list[str] = []
    lines.append("=" * 100)
    head = (f"TSec Panel  bench={token_hint}…  {meta.get('mode', '?')}/{meta.get('model', '?')}"
            f"  run@{run_info.get('started_at', '?')}  now {now}")
    lines.append(head)

    if plat:
        lines.append(f"平台: 通关 {plat['completed']}/{plat['total']} 题"
                     f" | flags {plat['flags']}/{plat['max_flags']}"
                     f" | 总分 {plat['score']}")
    else:
        # 离线回退：本地记录统计
        done = [t for t in tasks.values() if t.get("outcome") == "pass"]
        lines.append(f"平台: (离线) 本地通关 {len(done)} 题 | "
                     f"记录任务 {len(tasks)} | data: {runs_dir}")

    running = [t for t in tasks.values() if t.get("running")]
    lines.append(f"并行中: {len(running)} 题 | 记录目录: {runs_dir}")
    lines.append("-" * 100)

    # 运行中题目优先展示，其后按启动时间倒序放最近完成的
    order = sorted(tasks.values(),
                   key=lambda t: (not t.get("running", False),
                                  t.get("closed_at") or t.get("started_at") or ""),
                   reverse=False)
    shown = 0
    for t in order:
        if shown >= 14:
            lines.append(f"… 还有 {len(order) - shown} 题省略")
            break
        code = t["code"]
        if t.get("running"):
            started = t.get("started_at", "")
            dur = ""
            try:
                st = time.mktime(time.strptime(started, "%Y-%m-%d %H:%M:%S"))
                dur = f" 已跑 {_fmt_dur(max(0.0, time.time() - st))}"
            except (ValueError, TypeError):
                pass
            status = f"▶ RUNNING{dur}  attempt={t.get('attempt', '?')}  target={t.get('target', '?')}"
        elif t.get("outcome") == "pass":
            status = f"✔ PASS  flags {t.get('flags', '?')}  耗时 {_fmt_dur(t.get('duration') or 0)}"
        else:
            status = f"✘ FAIL  flags {t.get('flags', '?')}  耗时 {_fmt_dur(t.get('duration') or 0)}"
        lines.append(f"[{code}] {status}")
        if t.get("running") or t.get("outcome") == "pass":
            view = task_live_view(runs_dir, code)
            if view["running_cmds"]:
                for c in view["running_cmds"]:
                    lines.append(f"      ⚙ shell: {_one_line(c, 88)}")
            if view["last_output"]:
                lines.append(f"      ▸ 模型: {_one_line(view['last_output'], 88)}")
            elif view["last_thinking"]:
                lines.append(f"      ⤿ 思考: {_one_line(view['last_thinking'], 88)}")
        shown += 1

    lines.append("-" * 100)
    if events:
        lines.append("最近事件:")
        for e in events:
            payload = {k: v for k, v in e.items() if k not in ("ts", "type")}
            lines.append(f"  {e.get('ts', '')}  {e.get('type', '')}  "
                         f"{_one_line(json.dumps(payload, ensure_ascii=False), 78)}")
    lines.append("=" * 100)
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="TSec bench 本地观察面板")
    ap.add_argument("--dir", help="runs/<TOKEN> 目录（默认自动找最新）")
    ap.add_argument("--url", default=None, help="平台 BASE_URL（默认读 .env）")
    ap.add_argument("--interval", type=float, default=REFRESH_DEFAULT, help="刷新秒数，默认 5")
    ap.add_argument("--once", action="store_true", help="只渲染一帧（测试/脚本用）")
    args = ap.parse_args()

    runs_dir = args.dir or find_latest_runs()
    if not runs_dir or not os.path.isdir(runs_dir):
        print("未找到 runs/<TOKEN> 记录目录（用 --dir 指定，或先跑一次 run.sh）", file=sys.stderr)
        return 1
    base_url = args.url or env_base_url()

    clear = "\033[H\033[2J"  # ANSI 清屏定位
    while True:
        try:
            frame = render(runs_dir, base_url)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            frame = f"[panel error] {type(e).__name__}: {e}"
        sys.stdout.write(clear + frame + "\n")
        sys.stdout.flush()
        if args.once:
            return 0
        try:
            time.sleep(max(1.0, args.interval))
        except KeyboardInterrupt:
            print("\npanel exited")
            return 0


if __name__ == "__main__":
    sys.exit(main())
