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
    SOLVER_MAX_TURNS default 60
    SOLVER_MAX_MINUTES default 40
    SOLVER_CMD_TIMEOUT default 120   seconds per single command
    SOLVER_OUTPUT_LIMIT default 12000 chars of command output fed back to the model

Exit code: 0 on normal completion (flag found, final answer, or budget exhausted).
"""

from __future__ import annotations

import json
import sys
import time

import common
from common import (  # 函数引用安全：内部读 common 自身全局，转发无陈旧问题
    build_system_prompt,
    find_flags,
    report_candidate,
    run_command,
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

    tools = [
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": (
                    "Run a shell command inside the challenge container and return combined "
                    "stdout/stderr. Non-interactive commands only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "Shell command to execute."},
                        "timeout_seconds": {
                            "type": "integer",
                            "description": f"Kill the command after this many seconds (default {common.CMD_TIMEOUT}).",
                        },
                    },
                    "required": ["command"],
                },
            },
        }
    ]

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
        f"max_turns={common.MAX_TURNS} max_minutes={common.MAX_MINUTES}",
        flush=True,
    )

    start = time.time()
    deadline = start + common.MAX_MINUTES * 60
    totals = common.TOTALS
    totals.update({"prompt": 0, "completion": 0, "calls": 0, "llm_errors": 0})
    nudges = 0
    last_prompt_tokens = 0
    turn = 0

    while turn < common.MAX_TURNS and time.time() < deadline:
        turn += 1
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
            for flag in find_flags(content, include_generic=True):
                report_candidate(flag)

        if not calls:
            if find_flags(content, include_generic=True):
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
                command = str(args.get("command", "")).strip()
                t = int(args.get("timeout_seconds") or common.CMD_TIMEOUT)
            except Exception as exc:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": f"invalid tool arguments: {exc}",
                    }
                )
                continue

            if not command:
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": "empty command rejected"}
                )
                continue

            t = max(5, min(t, 600))
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

            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": f"[{meta}]\n{out}"}
            )

        # Context guard: drop oldest tool exchanges when the prompt gets large.
        if last_prompt_tokens > 45000 or len(messages) > 140:
            head = messages[:2]
            tail = messages[-30:]
            while tail and tail[0].get("role") == "tool":
                tail.pop(0)
            messages[:] = (
                head
                + [
                    {
                        "role": "user",
                        "content": "[context trimmed: older tool exchanges removed; continue solving, reuse files you created]",
                    }
                ]
                + tail
            )
            last_prompt_tokens = 0
            print("[solver] context trimmed", flush=True)

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
