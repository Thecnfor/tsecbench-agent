#!/usr/bin/env python3
"""TSecBench thin-shell solver (v0, 自研 DeepSeek function-calling 循环).

契约原语已抽取至 solver/common.py（main.py / dual_main.py 共用单一真值源）；
本模块保留 v0 LLM 循环，并通过模块级 __getattr__ 转发契约状态，使
test_solver_contract.py 与 pi_main.py 的 `mod.XXX` 访问形态不变。

In benchmark mode it is launched by BoxPwnr as an *external solver*:

    BOXPWNR_PROMPT      full system prompt (role + target + rules + flag formats)
    BOXPWNR_TARGET_IP   target connection info (e.g. "172.17.0.1:32768")
    BOXPWNR_CONTAINER   Kali container managed by BoxPwnr (docker exec target)

The shell runs its own DeepSeek function-calling loop, executes every command
inside the challenge container via `docker exec`, and prints discovered flag
candidates to stdout (BoxPwnr harvests them from our stdout/stderr).

Config (solver/.env or environment; environment wins):
    LLM_API_KEY | DEEPSEEK_API_KEY   required
    LLM_BASE_URL     default https://api.deepseek.com/v1
    LLM_MODEL        default deepseek-chat
    SOLVER_MAX_TURNS default 60（仅 BoxPwnr 直跑模式的保险丝；编排器模式
                     以时间片为预算，不按轮数截断）
    SOLVER_MAX_MINUTES default 40
    SOLVER_CMD_TIMEOUT default 120   seconds per single command
    SOLVER_OUTPUT_LIMIT default 12000 chars of command output fed back to the model

Exit code: 0 on normal completion (flag found, final answer, or budget exhausted).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import common
import state
from common import (  # 函数引用安全：内部读 common 自身全局，转发无陈旧问题
    build_system_prompt,
    find_flags,
    report_candidate,
    run_command,
    sanitize_tool_messages,
    setup_orch_mode,
    truncate,
    write_result,
)

# 常量 here-bind（导入后不再变，ruff 可静态解析；测试 monkeypatch main.API_KEY
# 时以 main 模块属性优先，语义与抽取前一致）。可变状态（TARGET_INFO/TASK_PROMPT/
# MAX_MINUTES …）不 here-bind，经下方 __getattr__ 实时转发 common。
API_KEY = common.API_KEY


# 纯量状态不 here-bind：经 __getattr__ 实时转发 common（setup_orch_mode 的
# 变更对 test_solver_contract / pi_main / 本模块循环全部立即可见）。
def __getattr__(name: str):  # noqa: ANN202, D103
    return getattr(common, name)


def _messages_as_events(messages: list[dict]) -> list[dict]:
    """把消息历史压成 prior 合成需要的事件流（命令 + 工具输出）。"""
    events: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                if fn.get("name") == "run_command":
                    events.append({"text": "$ " + str(args.get("command") or "")})
                else:
                    events.append({"text": f"{fn.get('name')} {args}"})
        elif role == "tool":
            events.append({"text": str(message.get("content") or "")})
    return events


def _persist_harness_state(messages: list[dict]) -> None:
    """跨 attempt 落盘：命令摘要 + 结构化 prior + 追加式 findings。"""
    if not common.SOLVER_TASK_PATH:
        return
    try:
        out_dir = Path(common.SOLVER_TASK_PATH).parent / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "harness_state.txt").write_text(
            common.summarize_history(messages, limit=16), encoding="utf-8"
        )
        events = _messages_as_events(messages)
        prior = state.synthesize_prior(events, sorted(common._reported))
        (out_dir / "prior.json").write_text(
            json.dumps(prior, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        blob = "\n".join(event["text"] for event in events)
        state.append_findings(out_dir / "findings.txt", state.extract_findings(blob))
    except OSError:
        pass


def _flag_nudge(common_mod, reported_before: int) -> str | None:
    """多 flag 进度：只在发现新 flag 且仍有剩余时追加一次结构化提示。"""
    task = getattr(common_mod, "TASK", {}) or {}
    remaining = int(task.get("flag_count", 1)) - int(task.get("correct_flag_count", 0))
    new_flags = len(common_mod._reported) - reported_before
    if remaining <= 0 or new_flags <= 0:
        return None
    outstanding = max(0, remaining - new_flags)
    return (
        f"{new_flags} new flag(s) found; {outstanding} still outstanding after submitting "
        "this one. Reuse confirmed credentials against every discovered service, and treat "
        "this flag as a new pivot. Do not stop."
    )


# --------------------------------------------------------------------- main ---
def main() -> int:
    if common.ORCH_MODE:
        setup_orch_mode()
    if not API_KEY:
        print(
            "[solver] FATAL: LLM_API_KEY is not set. Put it into solver/.env "
            "(LLM_API_KEY=sk-...) or export it before running.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    from openai import OpenAI  # imported lazily so config errors surface first

    client = OpenAI(api_key=API_KEY, base_url=common.BASE_URL, timeout=180.0, max_retries=2)

    tools = common.build_tool_specs()

    messages: list[dict] = [
        {"role": "system", "content": build_system_prompt()},
        {
            "role": "user",
            "content": (
                "Begin solving the challenge now."
                + (f"\nTarget connection info: {common.TARGET_INFO}" if common.TARGET_INFO else "")
                + "\nWork autonomously until you find the flag; when you have it, state it in a final answer."
            ),
        },
    ]

    print(
        f"[solver] model={common.MODEL} base_url={common.BASE_URL} "
        f"container={common.CONTAINER or '(local)'} "
        f"max_turns={'time-bounded' if common.ORCH_MODE else common.MAX_TURNS} "
        f"max_minutes={common.MAX_MINUTES}",
        flush=True,
    )

    start = time.time()
    deadline = start + common.MAX_MINUTES * 60
    totals = common.TOTALS
    totals.update({"prompt": 0, "completion": 0, "calls": 0, "llm_errors": 0})
    nudges = 0
    closing_nudged = False
    last_prompt_tokens = 0
    turn = 0

    while turn < common.turn_limit() and time.time() < deadline:
        turn += 1
        reported_before = len(common._reported)
        # 历史卫生：任何 user 插话/裁剪都可能切断 tool_calls 组 → DeepSeek 400 永久中毒
        messages[:] = sanitize_tool_messages(messages)
        try:
            resp = client.chat.completions.create(
                model=common.MODEL,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as exc:  # network / rate limit / bad request
            totals["llm_errors"] += 1
            print(f"[solver] LLM call failed ({type(exc).__name__}): {exc}", flush=True)
            time.sleep(5)
            continue

        usage = getattr(resp, "usage", None)
        if usage is not None:
            totals["prompt"] += usage.prompt_tokens or 0
            totals["completion"] += usage.completion_tokens or 0
            totals["calls"] += 1
            last_prompt_tokens = usage.prompt_tokens or last_prompt_tokens

        message = resp.choices[0].message
        calls = message.tool_calls or []
        content = (message.content or "").strip()

        assistant_msg: dict = {"role": "assistant", "content": message.content or ""}
        if calls:
            assistant_msg["tool_calls"] = [call.model_dump() for call in calls]
        messages.append(assistant_msg)

        if content:
            print(f"[solver] assistant (turn {turn}): {content[:1500]}", flush=True)
            for flag in find_flags(content):
                report_candidate(flag)

        if not calls:
            if find_flags(content):
                print(f"[solver] final answer with flag at turn {turn}", flush=True)
                break
            nudges += 1
            print(f"[solver] plain-text turn without tool call (nudge {nudges})", flush=True)
            messages.append(
                {
                    "role": "user",
                    "content": "Continue the work: call run_command for the next step. Do not just describe intentions.",
                }
            )
            if nudges >= 3:
                print("[solver] too many plain-text turns; stopping", flush=True)
                break
            continue

        for call in calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except Exception as exc:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": f"invalid tool arguments: {exc}",
                    }
                )
                continue

            if call.function.name in ("recon", "rea", "kb"):
                tool = call.function.name
                print(
                    f"[solver] turn {turn} {tool} {args.get('mode') or args.get('query', '')[:40]}",
                    flush=True,
                )
                if tool == "recon":
                    result = common.run_recon(args)
                elif tool == "rea":
                    result = common.run_rea(args)
                else:
                    result = common.run_kb(args)
                text = truncate(json.dumps(result, ensure_ascii=False))
                for flag in find_flags(text):
                    report_candidate(flag)
                remaining = common.remaining_seconds()
                note = f" | slice_time_left={int(remaining)}s" if remaining is not None else ""
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": f"[{tool}{note}]\n{text}",
                    }
                )
                continue

            command = str(args.get("command", "")).strip()
            try:
                requested = int(args.get("timeout_seconds") or common.CMD_TIMEOUT)
            except (TypeError, ValueError):
                requested = common.CMD_TIMEOUT

            if not command:
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": "empty command rejected"}
                )
                continue

            remaining = common.remaining_seconds()
            t = common.effective_command_timeout(requested, remaining)
            print(f"[solver] turn {turn} $ {command}", flush=True)
            result = run_command(command, t)
            out = truncate(result["output"])
            meta = f"exit_code={result['exit_code']} seconds={result['seconds']}" + (
                " TIMEOUT" if result["timed_out"] else ""
            )
            print(f"[solver]   -> {meta}", flush=True)
            if out.strip():
                print(out, flush=True)
            for flag in find_flags(result["output"]):
                report_candidate(flag)

            remaining = common.remaining_seconds()
            # 剩余预算是 harness 状态：每条结果都带，模型自定节奏（不靠提示词叮嘱）
            note = f" | slice_time_left={int(remaining)}s" if remaining is not None else ""
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": f"[{meta}{note}]\n{out}"}
            )

        # 收尾提示必须在整组 tool 结果之后追加：插在组中间会让历史永久 400。
        nudge = _flag_nudge(common, reported_before)
        if nudge:
            messages.append({"role": "user", "content": nudge})
        remaining = common.remaining_seconds()
        if remaining is not None and remaining < 60 and not closing_nudged:
            closing_nudged = True
            print("[solver] closing nudge: slice nearly over", flush=True)
            messages.append(
                {
                    "role": "user",
                    "content": "Time slice is nearly over. Stop opening new threads: verify any "
                    "candidate flag you already have and output it now. If nothing is confirmed, "
                    "write your best hypothesis and next step to /root/notes.md.",
                }
            )

        # Context guard: drop oldest tool exchanges when the prompt gets large.
        if last_prompt_tokens > common.TRIM_TOKENS or len(messages) > 140:
            head = messages[:2]
            tail = messages[-common.TRIM_KEEP_MSGS :]
            while tail and tail[0].get("role") == "tool":
                tail.pop(0)
            kept = "[context trimmed: older tool exchanges removed; continue solving, reuse files you created]"
            digest = common.summarize_history(messages[2 : max(2, len(messages) - 30)])
            if digest:
                kept += "\n" + digest
            messages[:] = head + [{"role": "user", "content": kept}] + tail
            last_prompt_tokens = 0
            print("[solver] context trimmed", flush=True)

        # 每轮回写状态：即便下一拍被片超时杀掉，harness 也知道做过什么
        _persist_harness_state(messages)

    elapsed = int(time.time() - start)
    print(
        f"[solver] finished: turns={turn} llm_calls={totals['calls']} llm_errors={totals['llm_errors']} "
        f"prompt_tokens={totals['prompt']} completion_tokens={totals['completion']} elapsed={elapsed}s "
        f"candidates={sorted(common._reported)}",
        flush=True,
    )
    write_result("solved" if common._reported else "failed")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
