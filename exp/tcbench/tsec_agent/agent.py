#!/usr/bin/env python3
"""TSec Benchmark 自动跑分 agent 主循环。

- 模型：deepseek-v4.1-flash-expires-on-0910（OpenAI 兼容网关）
- 工具：仅一个 `shell`（异步后台执行），无 MCP、无 subagent。
- 流程：VPN 预检 → 拉题列表 → 逐题 start/渗透/submit/close → 总结。

用法：
  python3 agent.py            # 运行到完成或超时
  python3 agent.py --smoke    # 冒烟测试：仅验证网关连通 + shell 工具
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from openai import OpenAI, APIError, APIConnectionError, APITimeoutError, RateLimitError

from config import CFG
from prompt import SYSTEM_PROMPT, TASK_TEMPLATE, CLOSING_SUMMARY_PROMPT
from shell_tool import SHELL_TOOL_SCHEMA, dispatch_shell, set_task_cwd, get_task_cwd
from records import init_records, rec
import bench
from bench import PlatformError

import threading

LOG_PATH = "agent.log"

# 题目启动时间戳（code -> unix ts），用于 task_close 计算 duration
TASK_START_TS: dict[str, float] = {}


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def make_client() -> OpenAI:
    return OpenAI(
        base_url=CFG.llm_base_url,
        api_key=CFG.llm_api_key,
        timeout=CFG.request_timeout,
        max_retries=0,  # 自行控制重试
    )


def call_llm(client: OpenAI, messages: list, use_tools: bool = True) -> object:
    """chat completion 调用，按 AGENT_LLM_RETRY_MODE 重试。

    - forever（默认）：连接/超时/限流/5xx 无限指数退避（封顶 llm_retry_max_delay），
      直到成功或任务时限；每 10 次记一条日志避免刷屏。
    - finite：最多 api_max_retries 次后抛出。
    - 4xx（认证/参数错误，非 429）：立即抛出，不重试。
    - use_tools=False：复盘总结轮用——不下发工具，模型只能输出文本。
    """
    delay = 2.0
    attempt = 0
    while True:
        attempt += 1
        try:
            kwargs = dict(
                model=CFG.model,
                messages=messages,
                temperature=0.2,
            )
            if use_tools:
                kwargs["tools"] = [SHELL_TOOL_SCHEMA]
                kwargs["tool_choice"] = "auto"
            return client.chat.completions.create(**kwargs)
        except (RateLimitError, APITimeoutError, APIConnectionError, APIError) as e:
            # 4xx 认证/参数错误（除 429 限流）重试无意义 → 立即失败
            status = getattr(e, "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                log(f"LLM fatal client error ({status}): {e}")
                raise
            # 网络类错误：按模式决定
            if CFG.llm_retry_mode != "forever" and attempt >= CFG.api_max_retries:
                raise
            # 任务时限到了就别再等
            rem = remaining_sec()
            if rem is not None and rem <= CFG.llm_retry_max_delay:
                raise RuntimeError(f"deadline approaching, abort retries: {e}")
            wait = min(delay, CFG.llm_retry_max_delay)
            if attempt == 1 or attempt % 10 == 0:
                log(f"LLM call failed (attempt {attempt}, {CFG.llm_retry_mode} mode): "
                    f"{type(e).__name__}: {e}; retry in {wait:.0f}s")
                rec().event("llm_retry", {"attempt": attempt,
                                                  "error": f"{type(e).__name__}: {e}",
                                                  "next_delay_sec": wait})
            time.sleep(wait)
            delay = min(delay * 2, CFG.llm_retry_max_delay)


def remaining_sec() -> float | None:
    if CFG.deadline_sec <= 0:
        return None
    return CFG.deadline_sec - (time.time() - START_TS)


def start_platform_watcher(interval: float = 20.0) -> None:
    """后台轮询平台题列表：题目容器生命周期与 flag 数变化写入 trace。"""
    import threading
    import urllib.request

    if os.environ.get("AGENT_PLATFORM_WATCH", "1") in ("0", "off", "false"):
        rec().event("platform_watcher_disabled", {})
        return

    def _fetch() -> list | None:
        req = urllib.request.Request(
            f"{CFG.benchmark_base_url.rstrip('/')}/openapi/v1/challenges",
            headers={"BENCHMARK_TOKEN": CFG.benchmark_token},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _watch():
        prev: dict = {}
        first = True
        while True:
            try:
                items = _fetch()
            except Exception as e:
                rec().event("platform_poll_error", {"error": f"{type(e).__name__}: {e}"})
                time.sleep(interval)
                continue
            cur = {c["unique_code"]: c for c in items}
            if first:
                rec().event("platform_snapshot", {
                    "total": len(cur),
                    "completed": [k for k, v in cur.items() if v["is_completed"]],
                })
                first = False
            for code, c in cur.items():
                p = prev.get(code)
                if p is None:
                    continue
                if p["container_status"] != c["container_status"]:
                    rec().event("challenge_change", {"code": code, "field": "container_status",
                                             "old": p["container_status"],
                                             "new": c["container_status"]})
                if p["correct_flag_count"] != c["correct_flag_count"]:
                    rec().event("challenge_change", {
                        "code": code, "field": "flags",
                        "old": f"{p['correct_flag_count']}/{p['flag_count']}",
                        "new": f"{c['correct_flag_count']}/{c['flag_count']}",
                        "completed": c["is_completed"]})
                if p["is_completed"] != c["is_completed"]:
                    rec().event("challenge_change", {"code": code, "field": "is_completed",
                                                     "old": p["is_completed"],
                                                     "new": c["is_completed"]})
            prev = cur
            time.sleep(interval)

    threading.Thread(target=_watch, daemon=True).start()
    rec().event("platform_watcher_started", {"interval_sec": interval})


def smoke_test(client: OpenAI) -> int:
    """验证：网关连通、模型可用、shell 工具可用、VPN 预检。"""
    ok = True

    # 1. 模型网关
    try:
        r = client.chat.completions.create(
            model=CFG.model,
            messages=[{"role": "user", "content": "回复两个字：正常"}],
            max_tokens=512,  # 推理模型（deepseek-flash）需预留思考 token
        )
        log(f"model gateway OK: {r.choices[0].message.content!r}")
    except Exception as e:
        log(f"model gateway FAILED: {type(e).__name__}: {e}")
        ok = False

    # 2. shell 工具
    out = dispatch_shell({"action": "run", "command": "echo shell-ok && uname -m", "wait_ms": 3000})
    if "shell-ok" in out:
        log(f"shell tool OK: {out.splitlines()[-1]}")
    else:
        log(f"shell tool FAILED: {out}")
        ok = False

    out = dispatch_shell({
        "action": "run",
        "command": f"curl -s -m 10 {CFG.vpn_health_url}",
        "wait_ms": 12000,
    })
    vpn_ok = '"status"' in out and '"ok"' in out
    if vpn_ok:
        log(f"VPN precheck OK: {out.strip().splitlines()[-1]}")
    elif CFG.mode == "hosted":
        log(f"VPN precheck FAILED (hosted mode is mandatory): {out}")
        ok = False
    else:
        # 本地模式：模型走公网，VPN 仅靶场渗透需要；开发调试可未连
        log(f"VPN precheck not OK (local mode, non-fatal for smoke; "
            f"run.sh/entrypoint enforces it before full runs): {out.splitlines()[0] if out else 'empty'}")

    return 0 if ok else 1


def solve_task(client: OpenAI, code: str, task_card: str, budget: int,
               conv) -> tuple[bool, str, int, list]:
    """单题独立上下文：解题直到模型输出最终总结（无工具调用）或轮次预算耗尽。

    返回 (got_summary, last_content, turns_used, messages)——messages 交还调用方用于
    通关后的复盘总结（同上下文），复盘完成即销毁。
    对话全量、忠实落盘（conversation.jsonl）：发送什么、输出什么，原样记录不裁剪。
    """
    messages: list = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task_card},
    ]
    # 忠实记录本题输入（system + 任务卡）
    conv.write("system", turn=f"{code}:0", content=SYSTEM_PROMPT)
    conv.write("user", turn=f"{code}:0", content=task_card)
    last_content = ""
    turns_used = 0
    for turn in range(1, budget + 1):
        if (rem := remaining_sec()) is not None and rem <= 0:
            return False, last_content, turns_used, messages
        try:
            resp = call_llm(client, messages)
        except Exception as e:
            log(f"[{code}] fatal LLM error: {type(e).__name__}: {e}")
            conv.write("error", turn=f"{code}:{turn}", error=f"{type(e).__name__}: {e}")
            return False, last_content, turns_used, messages
        turns_used += 1

        msg = resp.choices[0].message
        tlabel = f"{code}:{turn}"
        tool_calls_raw = getattr(msg, "tool_calls", None) or []
        # AI 输出全量落盘：thinking / output / tool_calls 原文
        conv.write(
            "assistant",
            turn=tlabel,
            thinking=getattr(msg, "reasoning_content", None),
            output=msg.content,
            tool_calls=[{"id": tc.id, "name": tc.function.name,
                         "arguments": tc.function.arguments} for tc in tool_calls_raw],
            usage=getattr(resp, "usage", None),
        )
        if msg.content:
            last_content = msg.content

        clean = {"role": msg.role}
        if msg.content:
            clean["content"] = msg.content
        if tool_calls_raw:
            clean["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls_raw
            ]
        messages.append(clean)

        if not tool_calls_raw:
            # 本题上下文的最终总结 → 交还控制器
            return True, msg.content or "", turns_used, messages

        for tc in tool_calls_raw:
            args = {}
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                result = f"error: invalid JSON arguments: {tc.function.arguments!r}"
            else:
                if tc.function.name == "shell":
                    result = dispatch_shell(args)
                else:
                    result = f"error: unknown tool {tc.function.name!r}"
            # 工具结果全量落盘（与 assistant 条目通过 tool_call_id 关联）
            conv.write("tool", turn=tlabel, tool_call_id=tc.id, tool=tc.function.name,
                       arguments=args, result=result)
            log(f"tool#{tc.id[-6:]} {tc.function.name} -> {result.splitlines()[0][:160] if result else ''}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    log(f"[{code}] per-task turn budget ({budget}) exhausted")
    return False, last_content, turns_used, messages


def closing_summary(client: OpenAI, code: str, messages: list, conv) -> str:
    """通关后复盘：在同一题上下文里追加一条总结请求（无工具），回复作为普通消息
    与其它对话一样忠实写入 conversation.jsonl。返回总结文本。"""
    user_msg = {"role": "user", "content": CLOSING_SUMMARY_PROMPT}
    messages.append(user_msg)
    conv.write("user", turn=f"{code}:closing", content=CLOSING_SUMMARY_PROMPT)
    try:
        resp = call_llm(client, messages, use_tools=False)
    except Exception as e:
        log(f"[{code}] closing summary LLM error: {type(e).__name__}: {e}")
        conv.write("error", turn=f"{code}:closing", error=f"{type(e).__name__}: {e}")
        return ""
    msg = resp.choices[0].message
    assistant_msg = {"role": "assistant", "content": msg.content}
    messages.append(assistant_msg)
    conv.write("assistant", turn=f"{code}:closing",
               thinking=getattr(msg, "reasoning_content", None),
               output=msg.content,
               kind_note="closing_summary",   # 标记本条为通关复盘
               usage=getattr(resp, "usage", None))
    log(f"[{code}] closing summary recorded ({len(msg.content or '')} chars)")
    return msg.content or ""


def _task_target(state: dict) -> str:
    addr = state.get("container_addr") or []
    if isinstance(addr, str):
        return addr
    return addr[0] if addr else ""


def run(client: OpenAI) -> int:
    """控制器主循环：选题 → start → 单题独立上下文解题 → 校验 → close → 下一题。

    平台编排由 Python 硬编码（可靠），模型只在单题上下文里解题（上下文隔离）。
    所有记录持久化到当前 bench 目录（records.py）。
    """
    R = init_records(mode=CFG.mode, model=CFG.model)
    R.event("run_start", {"mode": CFG.mode, "model": CFG.model,
                          "max_turns": CFG.max_turns, "deadline_sec": CFG.deadline_sec})
    R.run_start(mode=CFG.mode, model=CFG.model, max_turns=CFG.max_turns)
    log(f"records dir: {R.dir}")
    start_platform_watcher()
    task_budget = int(os.environ.get("AGENT_TASK_MAX_TURNS", "60"))
    task_filter = os.environ.get("AGENT_TASK_FILTER", "").strip()
    results: dict[str, str] = {}   # code -> outcome(pass/fail)
    attempts: dict[str, int] = {}
    total_turns = 0

    def snapshot() -> list | None:
        try:
            return bench.fetch_challenges()
        except PlatformError as e:
            if e.code == "task_not_found":
                raise
            log(f"platform fetch error: {e}")
            rec().event("platform_poll_error", {"error": str(e)})
            return None
        except Exception as e:
            log(f"platform fetch error: {type(e).__name__}: {e}")
            rec().event("platform_poll_error", {"error": f"{type(e).__name__}: {e}"})
            return None

    # 并行调度：每题独立线程 + 独立上下文 + 独立工作目录
    try:
        _run_parallel(client, task_budget, task_filter, results, attempts, snapshot)
    except Exception as e:
        # 调度层任何未预期异常都不允许"静默死亡"：落日志并收尾
        rec().event("run_finish", {"reason": "controller_error",
                                   "error": f"{type(e).__name__}: {e}"})
        rec().run_finish(reason="controller_error", error=f"{type(e).__name__}: {e}",
                         results=results)
        log(f"controller error: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        raise SystemExit(4) from e

    final = snapshot()
    _print_summary(results, final)
    rec().event("run_finish", {"reason": "controller_done", "results": results})
    rec().run_finish(reason="controller_done", results=results)
    return 0


def _task_workdir(code: str) -> str:
    """每题独立工作目录（线程局部 cwd 的落点），避免并行读写冲突。"""
    base = os.environ.get("TASK_WORKDIR") or (
        "/work/tasks" if os.access("/work", os.W_OK) else os.path.join(os.getcwd(), "tasks"))
    d = os.path.join(base, code)
    os.makedirs(d, exist_ok=True)
    return d


def _run_parallel(client, task_budget, task_filter, results, attempts, snapshot) -> None:
    """并行调度器：并发解多题，自动扩容直到平台拒绝（max active）。

    - 并发上限 AGENT_MAX_PARALLEL（默认 5）为本地意愿；平台 start 返回
      "max active" 时收缩为平台实际活跃数，之后随完成动态补齐。
    - 防冲突：每题独立上下文 + 独立线程 + 独立工作目录（thread-local cwd）；
      results/attempts 由锁保护；平台 start/close 由调度线程串行执行。
    - 调度线程绝不因单个题的异常崩溃（worker 异常记 fail 继续）。
    """
    lock = threading.Lock()
    running: dict[str, threading.Thread] = {}
    cap = int(os.environ.get("AGENT_MAX_PARALLEL", "5"))

    def worker(task: dict, addr: str, attempt: int) -> None:
        code = task["unique_code"]
        set_task_cwd(_task_workdir(code))
        last = ""
        state = None
        closing_text = ""
        conv = rec().conversation(code)
        messages: list | None = None
        try:
            card = TASK_TEMPLATE.format(
                code=code, difficulty=task["difficulty"], level=task["level"],
                flag_count=task["flag_count"], total_score=task["total_score"],
                description=task["description"], target=addr,
                workdir=get_task_cwd() or _task_workdir(code),
            )
            budget = task_budget
            if rem := remaining_sec():
                budget = max(10, min(task_budget, int(rem // 6)))
            _, last, _, messages = solve_task(client, code, card, budget, conv)

            # 校验平台状态（快照失败重试，避免误判）
            for _try in range(3):
                items_now = snapshot()
                if items_now is not None:
                    state = bench.challenge_state(items_now, code)
                    if state is not None:
                        break
                time.sleep(4)

            # 通关后：同一上下文直接追加复盘请求，总结像普通消息一样落 conversation.jsonl
            if state and state["is_completed"] and messages is not None:
                closing_text = closing_summary(client, code, messages, conv)
        except Exception as e:
            log(f"[{code}] worker error: {type(e).__name__}: {e}")
            rec().event("task_error", {"code": code, "error": f"{type(e).__name__}: {e}"})
        finally:
            messages = None  # 该题上下文销毁（引用丢弃，随 GC 回收）
            set_task_cwd("")  # 线程结束清 thread-local
            closed_at = time.strftime("%Y-%m-%d %H:%M:%S")
            completed = bool(state and state["is_completed"])
            try:
                bench.close_challenge(code)
                rec().event("challenge_change", {"code": code, "field": "controller_close",
                                                 "old": "available", "new": "closed",
                                                 "completed": completed})
            except PlatformError as e:
                if e.code != "invalid_state":
                    log(f"[{code}] close error: {e}")
            except Exception as e:
                log(f"[{code}] close network error: {type(e).__name__}: {e}")
                rec().event("platform_poll_error",
                            {"where": "close", "code": code, "error": str(e)})
            outcome = "pass" if completed else "fail"
            with lock:
                results[code] = outcome
            # 题目生命周期：关闭时间 + 耗时 + 结果（task_close 对应 task_start）
            rec().task_event(code, "task_close",
                             closed_at=closed_at,
                             duration_sec=round(time.time() - TASK_START_TS.get(code, time.time()), 1),
                             outcome=outcome,
                             flags=(f"{state['correct_flag_count']}/{state['flag_count']}" if state else "?"),
                             summary=(closing_text or last or ""),
                             workdir_summary=(last or ""))
            log(f"=== task {code}: {outcome} "
                f"{'(' + str(state['correct_flag_count']) + '/' + str(state['flag_count']) + ')' if state else ''} ===")
            with lock:
                running.pop(code, None)

    while True:
        if (rem := remaining_sec()) is not None and rem <= 60:
            log("deadline near; stopping scheduler")
            break

        # 回收已完成 worker
        with lock:
            for code in [c for c, t in running.items() if not t.is_alive()]:
                running.pop(code, None)

        items = snapshot()
        if items is None:
            time.sleep(8)
            continue
        for code in bench.release_completed(items):
            rec().event("challenge_change", {"code": code, "field": "controller_close",
                                             "old": "available", "new": "closed",
                                             "note": "released after completion"})
            with lock:
                results.setdefault(code, "pass")
        items = snapshot() or items

        # 候选：未通关、不在跑、未超重试上限、通过过滤
        def eligible(c: dict) -> bool:
            if c["is_completed"]:
                return False
            with lock:
                if c["unique_code"] in running:
                    return False
                if results.get(c["unique_code"]) == "pass":
                    return False
                if attempts.get(c["unique_code"], 0) >= 2:
                    return False
            return not (task_filter and c["unique_code"] != task_filter)

        candidates = sorted(
            [c for c in items if eligible(c)],
            key=lambda c: (0 if c["container_status"] == "available" else 1,
                           bench.DIFF_RANK.get(c["difficulty"], 9),
                           c.get("level", 9), c["unique_code"]),
        )

        if not candidates and not running:
            break  # 全部完成或全部放弃

        # 扩容：直到槽位用尽或平台拒绝（max active）
        for task in candidates:
            with lock:
                if len(running) >= cap:
                    break
            code = task["unique_code"]
            addr = _task_target(task)
            if task["container_status"] != "available" or not addr:
                started = False
                slot_full = False
                task_ended = False
                for _try in range(3):
                    try:
                        resp = bench.start_challenge(code)
                        addr = _task_target(resp) or addr
                        started = True
                        break
                    except PlatformError as e:
                        if e.code == "invalid_state" and "max active" in e.message:
                            # 平台拒绝 → 收缩并发到平台实际容量，本轮不再 start
                            active = bench.active_count(snapshot() or items)
                            with lock:
                                cap = max(len(running), active)
                            log(f"platform max-active reached; cap -> {cap}")
                            rec().event("parallel_cap", {"cap": cap, "reason": "max_active"})
                            slot_full = True
                            break
                        elif e.code == "invalid_state":
                            log(f"platform task ended: {e.message}")
                            task_ended = True
                            break
                        elif e.code == "resource_unavailable":
                            time.sleep(5)
                            continue
                        else:
                            log(f"[{code}] start error: {e}")
                            break
                    except Exception as e:
                        log(f"[{code}] start network error (try {_try + 1}): {type(e).__name__}: {e}")
                        rec().event("platform_poll_error",
                                            {"where": "start", "code": code, "error": str(e)})
                        time.sleep(5)
                if task_ended:
                    return  # 任务整体结束，交由 run() 收尾
                if slot_full:
                    break  # 名额满了，等 worker 完成后再扩
                if not started:
                    # start 失败也计入 attempts，避免同一题无限重试死循环
                    with lock:
                        attempts[code] = attempts.get(code, 0) + 1
                        results[code] = "fail" if attempts[code] >= 2 else results.get(code, "fail")
                    continue
            if not addr:
                log(f"[{code}] no container_addr from start")
                with lock:
                    attempts[code] = attempts.get(code, 0) + 1
                    results[code] = "fail"
                continue

            with lock:
                attempts[code] = attempts.get(code, 0) + 1
                attempt_no = attempts[code]
            # 题目生命周期：启动时间（task_start，对应 task_close 的 closed_at/duration）
            rec().task_event(code, "task_start",
                             started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                             target=addr, attempt=attempt_no,
                             parallel=len(running) + 1,
                             flags=f"0/{task['flag_count']}")
            TASK_START_TS[code] = time.time()
            log(f"=== task {code} attempt {attempt_no} target={addr} "
                f"[parallel {len(running) + 1}/{cap}] ===")
            t = threading.Thread(target=worker, args=(task, addr, attempt_no),
                                 name=f"task-{code}", daemon=True)
            with lock:
                running[code] = t
            t.start()
            time.sleep(0.3)  # 平滑扩容，避免瞬时打满平台

        # 有 worker 在跑则等待；空转保护
        if running:
            time.sleep(2)
        elif candidates:
            time.sleep(5)

    # 收尾：等待所有 worker（最多 3 分钟/题剩余时限）
    for code, t in list(running.items()):
        t.join(timeout=180)
        if t.is_alive():
            log(f"[{code}] worker still running at shutdown; detached")


def _print_summary(results: dict, items: list | None) -> None:
    done = [c for c in (items or []) if c["is_completed"]]
    score = sum(c["total_score"] for c in done)
    total = len(items or [])
    lines = [
        "",
        "=" * 46,
        f"总进度：已通关 {len(done)}/{total} 题，总分 {score}",
        f"本轮结果：pass={sum(1 for v in results.values() if v == 'pass')} "
        f"fail={sum(1 for v in results.values() if v == 'fail')}",
        "=" * 46,
    ]
    print("\n".join(lines), flush=True)
    rec().event("final_summary", {"completed": len(done), "total": total,
                                          "score": score})


START_TS = time.time()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="gateway/shell/VPN connectivity check only")
    ap.add_argument("--mode", choices=["auto", "local", "hosted"], default=None,
                    help="override AGENT_MODE (auto-detect by default)")
    ap.add_argument("--task", default=None, metavar="CODE",
                    help="只解指定题目（调试用），如 --task e1-01")
    args = ap.parse_args()

    if args.mode:
        os.environ["AGENT_MODE"] = args.mode
    CFG.resolve()
    log(f"mode={CFG.mode} model={CFG.model} gateway={CFG.llm_base_url} bench={CFG.benchmark_base_url}")
    client = make_client()

    if args.smoke:
        return smoke_test(client)

    if args.task:
        os.environ["AGENT_TASK_FILTER"] = args.task
    return run(client)


if __name__ == "__main__":
    sys.exit(main())
