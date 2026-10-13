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
import calendar
import json
import os
import sys
import time
import urllib.request

# 注意：记录里的时间字符串（started_at/closed_at/ts）由容器写入，容器无时区 = **UTC**；
# 面板运行在宿主机（本地时区），解析必须用 calendar.timegm（按 UTC 解释），
# 否则 time.mktime 按本地时间解析会让"已跑时长"虚高一个时区差（如 +8h）。


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
                     summary=(r.get("summary") or "")[:200], running=False)
    return tasks


def _task_brief(t: dict) -> str:
    """PASS/FAIL 的单行紧凑摘要：code flags 耗时。"""
    dur = _fmt_dur(t.get("duration") or 0)
    flags = t.get("flags", "?")
    if t.get("outcome") == "pass":
        return f"{t['code']}({flags} {dur})"
    return f"{t['code']}({flags} {dur}{' 重试' if (t.get('attempt') or 1) > 1 else ''})"


def _wrap_bits(bits: list[str], width: int) -> list[str]:
    """把多个紧凑摘要拼成若干行（每行不超 width）。"""
    lines, cur = [], ""
    for b in bits:
        cand = f"{cur} {b}" if cur else b
        if len(cand) > width:
            if cur:
                lines.append(cur)
            cur = b
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


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


def render(runs_dir: str, base_url: str, rows: int = 40, cols: int = 120) -> str:
    """一屏自适应渲染：所有输出裁剪到 rows×cols 内，绝不导致终端滚动。

    布局（自上而下，空间不够按此顺序被裁剪）：
      头部（固定6行）→ RUNNING 详情（每题≤2行）→ FAIL 一行（聚合，每行多题）
      → PASS 一行（聚合，每行多题）→ 最近事件（尾部固定）
    """
    import shutil
    term = shutil.get_terminal_size(fallback=(cols, rows))
    cols = min(cols, max(40, term.columns - 1))
    # rows 预算尊重真实终端（下限仅 8 行兜底）；最终会硬裁到此值，绝不滚动
    rows = max(8, min(rows, term.lines - 1))

    now = time.strftime("%H:%M:%S")
    meta = load_meta(runs_dir)
    tasks = load_tasks(runs_dir)
    events = load_recent_events(runs_dir)
    plat = fetch_platform(runs_dir, base_url)

    def clip(s: str) -> str:
        s = (s or "").replace("\n", " ").replace("\r", " ")
        return s if len(s) <= cols else s[: cols - 1] + "…"

    run_info = (meta.get("runs") or [{}])[-1]
    token_hint = os.path.basename(runs_dir.rstrip("/"))[:8]

    running = [t for t in tasks.values() if t.get("running")]
    done = [t for t in tasks.values() if t.get("outcome") == "pass"]
    failed = [t for t in tasks.values() if t.get("outcome") == "fail"]
    # 分段解题：outcome=paused = 本段到点已关题、上下文保留，等队尾续跑
    paused = [t for t in tasks.values() if t.get("outcome") == "paused"]

    lines: list[str] = []
    # --- 头部（固定 6 行）---
    lines.append("=" * cols)
    lines.append(clip(f"bench={token_hint}… {meta.get('mode', '?')}/{meta.get('model', '?')}"
                      f"  run@{run_info.get('started_at', '?')}  now {now}"))
    if plat:
        lines.append(clip(f"平台: 通关 {plat['completed']}/{plat['total']} | flags "
                          f"{plat['flags']}/{plat['max_flags']} | 总分 {plat['score']}"))
    else:
        lines.append(clip(f"平台: (离线) 本地通关 {len(done)} | 记录 {len(tasks)} 题"))
    lines.append(clip(f"本地: ▶{len(running)} 在跑 ✔{len(done)} 通过 ✘{len(failed)} 失败"
                      + (f" ∥{len(paused)} 暂停" if paused else "")
                      + f" | dir: {runs_dir}"))
    lines.append("-" * cols)

    def _utc_ts(s: str) -> float | None:
        """记录里的 UTC 时间字符串 → epoch（解析失败返回 None）。"""
        try:
            return float(calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M:%S")))
        except (ValueError, TypeError):
            return None

    # --- RUNNING 详情（每题最多2行：状态 + 最近动作）---
    for t in running:
        if len(lines) >= rows - 2:
            break
        dur = ""
        st = _utc_ts(t.get("started_at", ""))
        if st is not None:
            dur = f" {_fmt_dur(max(0.0, time.time() - st))}"
        lines.append(clip(f"[{t['code']}] ▶{dur} att={t.get('attempt', '?')} "
                          f"{t.get('target', '?')}"))
        view = task_live_view(runs_dir, t["code"])
        act = (view["running_cmds"] or [None])[0] \
            or (view["last_cmd"] or None) \
            or (view["last_output"] or view["last_thinking"] or "")
        if act:
            lines.append(clip(f"    ↳ {act}"))

    # --- PAUSED 聚合（分段解题：本段到点已关题，等队尾续跑）---
    if paused and len(lines) < rows - 5:
        pause_bits = [_task_brief(t) for t in
                      sorted(paused, key=lambda x: x.get("closed_at") or "", reverse=True)]
        pause_line = "∥ PAUSED(待续跑): " + " ".join(pause_bits[:8])
        if len(pause_bits) > 8:
            pause_line += f" (+{len(pause_bits) - 8})"
        lines.append(clip(pause_line))

    # --- FAIL 聚合（每行多题，紧凑）---
    if failed and len(lines) < rows - 4:
        fail_bits = [_task_brief(t) for t in
                     sorted(failed, key=lambda x: x.get("closed_at") or "", reverse=True)]
        fail_line = "✘ FAIL: " + " ".join(fail_bits[:8])
        if len(fail_bits) > 8:
            fail_line += f" (+{len(fail_bits) - 8})"
        lines.append(clip(fail_line))

    # --- PASS 聚合（每行多题，紧凑，倒序最近优先）---
    if done and len(lines) < rows - 3:
        pass_bits = [_task_brief(t) for t in
                     sorted(done, key=lambda x: x.get("closed_at") or "", reverse=True)]
        head = "✔ PASS: "
        cur = head
        shown = 0
        for b in pass_bits:
            if len(cur) + len(b) + 1 > cols or shown >= 16:
                break
            cur = f"{cur} {b}" if cur != head else head + b
            shown += 1
        if len(pass_bits) > shown:
            cur += f" (+{len(pass_bits) - shown})"
        lines.append(clip(cur))

    # --- 最近事件（尾部 3 条）---
    if events and len(lines) < rows - 4:
        lines.append("-" * cols)
        for e in events[:3]:
            payload = {k: v for k, v in e.items() if k not in ("ts", "type")}
            lines.append(clip(f"  {e.get('ts', '')} {e.get('type', '')} "
                              f"{json.dumps(payload, ensure_ascii=False)}"))

    # --- 最终保险：硬裁到 rows，防止任何情况下的滚动 ---
    if len(lines) > rows:
        lines = lines[: rows - 1]
        lines.append(clip("…(更多内容已裁剪，完整数据见记录目录)"))
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
