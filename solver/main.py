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
    SOLVER_OUTPUT_LIMIT default 12000 chars of single tool output
    SOLVER_SPILL_CHARS default 6000  larger tool outputs go to out/tool_outputs/*
    SOLVER_TRIM_TOKENS default 90000 hard prompt-token threshold
    SOLVER_TRIM_SOFT_TOKENS default 60000 stale-turn elision threshold
    SOLVER_TRIM_TARGET_TOKENS default 40000 compaction low-water mark

Exit code: 0 on normal completion (flag found, final answer, or budget exhausted).
"""

from __future__ import annotations

import ipaddress
import json
import re
import sys
import time
from pathlib import Path

import common
import reflect
import skills
import state
from common import (  # 函数引用安全：内部读 common 自身全局，转发无陈旧问题
    build_system_prompt,
    find_flags,
    report_candidate,
    run_command,
    sanitize_tool_messages,
    setup_orch_mode,
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
                args, _error = common.parse_tool_arguments(fn.get("arguments") or "{}")
                if args is None:
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


def _fact_signature(text: str) -> set[str]:
    """高价值事实签名：凭据 / 新 host:port / flag。

    只猜出新路径不算进展（a-05 就是靠不断猜文件名把 no-fact 计时器
    反复重置，跑到 73 轮）。同主机不同路径视为同一事实。
    """
    findings = state.extract_findings(text)
    facts = {f"cred:{value}" for value in findings.get("credentials") or []}
    for endpoint in findings.get("endpoints") or []:
        match = re.match(r"https?://([^/]+)", endpoint) or re.match(r"([^/]+)", endpoint)
        if not match:
            continue
        host = match.group(1).lower().split(":")[0]
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            continue
        if address.is_private and not address.is_loopback:
            facts.add(f"host:{match.group(1).lower()}")
    for flag in find_flags(text):
        facts.add(f"flag:{flag}")
    return facts


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


def _structured_tool_message(
    turn: int,
    call,
    tool: str,
    args: dict,
    result: dict,
    *,
    turn_budget_left: int,
) -> tuple[dict, int]:
    label = args.get("mode") or str(args.get("query") or "")[:40]
    print(f"[solver] turn {turn} {tool} {label}", flush=True)
    raw_text = json.dumps(result, ensure_ascii=False)
    text = common.prepare_tool_output(
        raw_text,
        label=f"turn{turn}-{tool}",
        turn_budget_left=turn_budget_left,
    )
    known = result.get("known_issues") or []
    prefix = ""
    if known:
        prefix = (
            "KNOWN ISSUES (offline KB — apply first): "
            + " | ".join(str(item)[:120] for item in known[:4])
            + "\n"
        )
    for flag in find_flags(raw_text):
        report_candidate(flag)
    remaining = common.remaining_seconds()
    note = f" | slice_time_left={int(remaining)}s" if remaining is not None else ""
    return {
        "role": "tool",
        "tool_call_id": call.id,
        "content": f"[{tool}{note}]\n{prefix}{text}",
    }, len(text) + len(prefix)


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
    out_dir = Path(common.SOLVER_TASK_PATH).parent / "out" if common.SOLVER_TASK_PATH else None

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
    seen_facts: set[str] = set()
    factless_turns = 0
    last_fact_turn = 0
    fact_nudged = False
    no_fact_nudges = 0
    reflects = 0
    consecutive_errors = 0
    overflow_recoveries = 0
    turn = 0
    last_idea = ""
    known_terms: set[str] = set()
    seen_skills: set[str] = set()
    pending_hints: list[str] = []

    while turn < common.turn_limit() and time.time() < deadline:
        turn += 1
        reported_before = len(common._reported)
        turn_output_chars = 0
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
            if (
                common.is_context_overflow_error(exc)
                and overflow_recoveries < common.MAX_CONTEXT_RECOVERIES
            ):
                overflow_recoveries += 1
                before = common.conversation_tokens(messages)
                messages[:] = common.emergency_compact(messages)
                last_prompt_tokens = common.conversation_tokens(messages)
                common.log_context_event(
                    {
                        "turn": turn,
                        "compaction": "emergency",
                        "tokens_before": before,
                        "tokens_after": last_prompt_tokens,
                        "error": type(exc).__name__,
                    }
                )
                print(
                    f"[solver] context overflow -> emergency compact "
                    f"{before}->{last_prompt_tokens} tokens; retrying",
                    flush=True,
                )
                continue
            consecutive_errors += 1
            print(f"[solver] LLM call failed ({type(exc).__name__}): {exc}", flush=True)
            if consecutive_errors >= common.MAX_CONSECUTIVE_LLM_ERRORS:
                print(
                    f"[solver] {consecutive_errors} consecutive LLM errors; stopping",
                    flush=True,
                )
                break
            time.sleep(5)
            continue

        consecutive_errors = 0
        overflow_recoveries = 0

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
            last_idea = content[:300]
            for term in common.detect_known_terms(content):
                if term in known_terms:
                    continue
                known_terms.add(term)
                snippet = common.known_issue_snippet(term)
                if snippet:
                    pending_hints.append(
                        f"You explicitly identified {term}. Offline KB has a matching section — "
                        "apply its exact payload/syntax before inventing new paths:\n" + snippet
                    )
            skill = skills.match(content)
            if skill and skill["id"] not in seen_skills:
                seen_skills.add(skill["id"])
                pending_hints.append(skills.render(skill, common.TARGET_INFO.split(",")[0].strip()))
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
                    "content": "Continue the work: call run_command for the next step NOW. "
                    "A plain-text-only turn is wasted time; do not restate the plan.",
                }
            )
            if nudges >= common.MAX_PLAIN_NUDGES:
                print("[solver] too many plain-text turns; stopping", flush=True)
                break
            continue

        # 一轮内全部是结构化工具（recon/rea/kb）时并发执行；run_command 有顺序语义，
        # 混合或单独的 run_command 仍走下面的串行路径。
        prefetched: dict[str, dict] = {}
        if len(calls) > 1 and all(call.function.name in common.STRUCTURED_TOOLS for call in calls):
            parsed_batch = {
                call.id: common.parse_tool_arguments(call.function.arguments) for call in calls
            }
            if all(error is None for _args, error in parsed_batch.values()):
                results = common.run_structured_batch(
                    [
                        {"name": call.function.name, "args": parsed_batch[call.id][0]}
                        for call in calls
                    ]
                )
                prefetched = {call.id: result for call, result in zip(calls, results, strict=True)}

        for call in calls:
            args, error = common.parse_tool_arguments(call.function.arguments)
            if error:
                messages.append({"role": "tool", "tool_call_id": call.id, "content": error})
                continue
            if call.function.name not in ("run_command", "recon", "rea", "kb"):
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": (
                            f"unknown tool: {call.function.name!r}; "
                            "use run_command, recon, rea, or kb"
                        ),
                    }
                )
                continue

            if call.function.name in common.STRUCTURED_TOOLS:
                tool = call.function.name
                common.note_tool_use(tool)
                if call.id in prefetched:
                    result = prefetched.pop(call.id)
                elif tool == "recon":
                    result = common.run_recon(args)
                elif tool == "rea":
                    result = common.run_rea(args)
                elif tool == "stage":
                    result = common.run_stage(args)
                elif tool == "gremlin":
                    result = common.run_gremlin(args)
                elif tool == "gradio":
                    result = common.run_gradio(args)
                else:
                    result = common.run_kb(args)
                message, used = _structured_tool_message(
                    turn,
                    call,
                    tool,
                    args,
                    result,
                    turn_budget_left=common.TURN_OUTPUT_BUDGET - turn_output_chars,
                )
                turn_output_chars += used
                messages.append(message)
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
            common.note_tool_use("run_command")
            print(f"[solver] turn {turn} $ {command}", flush=True)
            result = run_command(command, t)
            raw_output = result["output"]
            out = common.prepare_tool_output(
                raw_output,
                label=f"turn{turn}-cmd",
                turn_budget_left=common.TURN_OUTPUT_BUDGET - turn_output_chars,
            )
            turn_output_chars += len(out)
            meta = f"exit_code={result['exit_code']} seconds={result['seconds']}" + (
                " TIMEOUT" if result["timed_out"] else ""
            )
            print(f"[solver]   -> {meta}", flush=True)
            if out.strip():
                print(out, flush=True)
            for flag in find_flags(raw_output):
                report_candidate(flag)

            remaining = common.remaining_seconds()
            # 剩余预算是 harness 状态：每条结果都带，模型自定节奏（不靠提示词叮嘱）
            note = f" | slice_time_left={int(remaining)}s" if remaining is not None else ""
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": f"[{meta}{note}]\n{out}"}
            )

        # 收敛守卫：长时间没有新资产/凭据/端点 → 先提示换面，再无事实则收尾。
        recent_tools = "\n".join(
            str(message.get("content") or "")
            for message in messages[-10:]
            if message.get("role") == "tool"
        )
        facts = _fact_signature(recent_tools)
        if facts - seen_facts:
            seen_facts |= facts
            factless_turns = 0
            last_fact_turn = turn
            fact_nudged = False
        else:
            factless_turns += 1
        if factless_turns >= common.NO_FACT_NUDGE_TURNS and not fact_nudged:
            fact_nudged = True
            no_fact_nudges += 1
            if no_fact_nudges >= common.MAX_NO_FACT_NUDGES and not common._reported:
                print(
                    f"[solver] second no-fact window ({factless_turns} turns); stopping",
                    flush=True,
                )
                break
            remaining = common.remaining_seconds()
            obs = None
            if reflect.should_reflect(reflects=reflects, remaining_s=remaining):
                obs = reflect.observe(
                    client,
                    common.MODEL,
                    getattr(common, "TASK", {}) or {},
                    messages,
                    sorted(common._reported),
                    remaining,
                )
            if obs:
                reflects += 1
                if out_dir is not None:
                    reflect.save(out_dir, obs)
                display = obs.get("lock") or (obs.get("angles") or [""])[0]
                print(
                    f"[solver] observer reflection #{reflects}: {display[:100]}",
                    flush=True,
                )
                messages.append({"role": "user", "content": reflect.render(obs)})
            else:
                print(
                    f"[solver] no new facts for {factless_turns} turns; switching surface",
                    flush=True,
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"No new asset/credential/endpoint in the last {factless_turns} turns. "
                            "This surface looks exhausted: switch to the next candidate from the "
                            "FAST PATH/KB, or stop and report your best hypothesis. Do not repeat "
                            "the same request shape."
                        ),
                    }
                )
        if turn >= common.MAX_TURNS_NO_FLAG and not common._reported:
            print(f"[solver] turn cap without flag ({turn}); stopping", flush=True)
            break
        if turn - last_fact_turn >= common.STALE_HARD_TURNS and not common._reported:
            print(
                f"[solver] stale surface for {turn - last_fact_turn} turns; stopping",
                flush=True,
            )
            break

        for hint in pending_hints:
            messages.append({"role": "user", "content": hint})
        pending_hints.clear()
        if turn % 15 == 0 and turn > 1:
            prior = common.TASK.get("prior") or {}
            assets = [str(item) for item in (prior.get("assets") or [])][:5]
            creds = [str(item) for item in (prior.get("credentials") or [])][:3]
            remaining = int(common.TASK.get("flag_count", 1)) - len(common._reported)
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"[STATE turn {turn}] flags={sorted(common._reported) or '-'}; "
                        f"remaining~{remaining}; products={sorted(known_terms) or '-'}; "
                        f"assets={assets or '-'}; creds={creds or '-'}."
                    ),
                }
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

        # Context budget: soft elision -> hard compaction -> (400) emergency.
        effective_tokens = last_prompt_tokens or common.conversation_tokens(messages)
        compaction = None
        if effective_tokens > common.TRIM_TOKENS or len(messages) > common.TRIM_MAX_MSGS:
            messages[:], dropped, before, after = common.compact_history(
                messages, target_tokens=common.TRIM_TARGET_TOKENS
            )
            last_prompt_tokens = after
            compaction = "hard"
            print(
                f"[solver] context compacted: dropped={dropped} tokens~{before}->{after}",
                flush=True,
            )
        elif effective_tokens > common.TRIM_SOFT_TOKENS:
            messages[:], elided = common.elide_stale_messages(
                messages, keep_recent_turns=common.KEEP_RECENT_TURNS
            )
            if elided:
                last_prompt_tokens = common.conversation_tokens(messages)
                compaction = "elide"
                print(f"[solver] stale turns elided: messages={elided}", flush=True)

        common.log_context_event(
            {
                "turn": turn,
                "prompt_tokens": last_prompt_tokens,
                "est_tokens": common.conversation_tokens(messages),
                "messages": len(messages),
                "tool_output_chars": turn_output_chars,
                "compaction": compaction,
            }
        )

        # 每轮回写状态：即便下一拍被片超时杀掉，harness 也知道做过什么
        _persist_harness_state(messages)

    elapsed = int(time.time() - start)
    print(
        f"[solver] finished: turns={turn} llm_calls={totals['calls']} llm_errors={totals['llm_errors']} "
        f"prompt_tokens={totals['prompt']} completion_tokens={totals['completion']} elapsed={elapsed}s "
        f"candidates={sorted(common._reported)}",
        flush=True,
    )
    write_result("solved" if common._reported else "failed", next_idea=last_idea)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
