#!/usr/bin/env python3
"""TSec Benchmark 自动跑分 agent 主循环。

- 模型：`deepseek-flash`（OpenAI 兼容网关；local/hosted 同名，hosted 走转写网关）
- 工具：仅一个 `shell`（异步后台执行），无 MCP、无 subagent。
- 流程：VPN 预检 → 拉题列表 → 逐题 start/渗透/submit/close → 总结。

用法：
  python3 agent.py            # 运行到完成或超时
  python3 agent.py --smoke    # 冒烟测试：仅验证网关连通 + shell 工具
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
import time

from openai import OpenAI, APIError, APIConnectionError, APITimeoutError, RateLimitError

from config import CFG
from prompt import (SYSTEM_PROMPT, TASK_TEMPLATE, CLOSING_SUMMARY_PROMPT,
                    RESUME_NOTE, GIVEUP_NOTE)
from shell_tool import (SHELL_TOOL_SCHEMA, dispatch_shell, parse_tool_arguments,
                        set_task_cwd, get_task_cwd)
from records import init_records, rec
import bench
from bench import PlatformError, TaskFinishedError

import threading

LOG_PATH = "agent.log"

# 全局停止事件：平台任务结束（already finished 等终态）时置位，
# 调度器/worker/watcher 全部收敛退出，不再重试。
STOP = threading.Event()

# 题目启动时间戳（code -> unix ts），用于 task_close 计算 duration
TASK_START_TS: dict[str, float] = {}


def platform_ended(where: str, e: Exception, results: dict | None = None) -> None:
    """平台终态统一入口：记录一次、置位停止事件。幂等。"""
    if STOP.is_set():
        return
    log(f"platform task FINISHED ({where}): {e} — stopping all agents/containers")
    rec().event("platform_ended", {"where": where, "error": str(e)})
    rec().run_finish(reason="platform_ended", detail=str(e), results=results or {})
    STOP.set()


# --- VPN 运行时守卫（弱点1修复）：VPN 中途挂 → 暂停派发新题、worker 挂起等待 ---
# 不判死刑：VPN 恢复即自动继续；等待期间不烧轮次、不消耗 attempts。
VPN_DOWN = threading.Event()


def _vpn_ok(timeout: float = 4.0) -> bool:
    """探测靶场预检地址（与 entrypoint 预检同一标准）。"""
    import urllib.request
    try:
        with urllib.request.urlopen(CFG.vpn_health_url, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
        return '"status"' in body and '"ok"' in body
    except Exception:
        return False


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _client_with_key(key: str, timeout: float | None = None) -> OpenAI:
    """按 key 构造 client；key=="" 表示匿名（请求不带 Authorization 头）。

    openai SDK（3.x，底层 httpx2）对空 key 的处理：传 "" 会发出 "Bearer " 空串而非
    不带头——用 httpx2 事件钩子把 Authorization 头整个剥掉，确保真正发出"无凭据"
    请求（部分转写网关不校验上游 key、自带平台配额）。
    """
    kw = dict(base_url=CFG.llm_base_url, timeout=timeout or CFG.request_timeout,
              max_retries=0)
    if key:
        return OpenAI(api_key=key, **kw)
    import httpx2

    def _strip_auth(request):
        # openai 3.x 构造时拒绝空 key（Missing credentials）→ 传占位串过构造，
        # 钩子在请求发出前把 Authorization 头整个剥掉 = wire 上真正无凭据
        if "Authorization" in request.headers:
            del request.headers["Authorization"]
        return request

    # httpx2 的 event_hooks 值必须是列表（httpx 才接受单函数）
    return OpenAI(api_key="anonymous-placeholder",
                  http_client=httpx2.Client(event_hooks={"request": [_strip_auth]}), **kw)


def make_client() -> OpenAI:
    if not CFG.llm_api_key:
        if CFG.mode == "hosted":
            # 托管下 key 可以为空：匿名（无 Authorization）本身就是候选之一，
            # 由 discover_gateway_key 探测网关是否接受，不再直接退出
            log("no api key configured; starting with anonymous client (hosted)")
            return _client_with_key("")
        # 本地模式无 key：公网 API 必然 401，干净报错退出（退出码 2 = 网关不可达）
        log(f"FATAL: model gateway credentials missing (mode={CFG.mode}, "
            f"base_url={CFG.llm_base_url}) — 本地模式需 .env/环境变量 DEEPSEEK_API_KEY；"
            f"若在托管环境看到此错，说明模式误判为 local，请检查 AGENT_MODE/hosted 网关探测")
        raise SystemExit(2)
    return _client_with_key(CFG.llm_api_key)


# key 候选发现只完整跑一次；401 热切换也只做一次，避免坏 key 死循环
_REAUTH_TRIED = False
_DISCOVERY_DONE = False
# 全局当前 client：任一线程 401 热切换后，其他线程下一次 call_llm 也用新 client
#（call_llm 的 client 参数是调用方传入的旧引用，不覆盖它切换就不生效）
_CURRENT_CLIENT: "OpenAI | None" = None


def _key_mask(k: str) -> str:
    """key 掩码：只显示尾 4 位，绝不输出完整 key（含 BENCHMARK_TOKEN 这类平台凭据）。"""
    return "****" + k[-4:] if k else "(none)"


def _key_candidates(exclude: set[str] | None = None) -> list[tuple[str, str]]:
    """网关 key 候选清单，按优先级：(标签, key)，key=="" 表示匿名。

    顺序（实测本届转写网关把 Authorization 透传上游 DeepSeek）：
    1. anonymous —— 匿名探测：部分部署网关自带平台配额、不透传鉴权；
    2. DEEPSEEK_API_KEY_HOSTED —— 显式配置的托管 key；
    3. BENCHMARK_TOKEN —— 平台注入的凭据，个别部署网关也认它；
    4. DEEPSEEK_API_KEY —— 真实 DeepSeek key（**最后**：用它=烧自己的额度）；
    5. 当前已配置 key（CFG.llm_api_key，如内置 tsec-bench）兜底。
    标签只带尾 4 位掩码；exclude 用于 401 热切换时跳过刚被拒的 key。
    """
    exclude = exclude or set()
    out: list[tuple[str, str]] = [("anonymous", "")]
    # seen 只预置 exclude（401 热切换要跳过的 key）；env 槽位按值去重，
    # 当前 key 若与某 env 值相同则由该槽位覆盖（同一个 key 不探两次），
    # 否则以 current-config 兜底在最后
    seen: set[str] = set(exclude)
    for name in ("DEEPSEEK_API_KEY_HOSTED", "BENCHMARK_TOKEN", "DEEPSEEK_API_KEY"):
        v = os.environ.get(name, "")
        if v and v not in seen:
            seen.add(v)
            out.append((f"env:{name}({_key_mask(v)})", v))
    if CFG.llm_api_key and CFG.llm_api_key not in seen:
        out.append((f"current-config({_key_mask(CFG.llm_api_key)})", CFG.llm_api_key))
    return out


def discover_gateway_key(cause: str = "smoke", exclude: set[str] | None = None,
                         probe_all: bool = False) -> OpenAI | None:
    """按候选清单探测网关 key，返回可用 key 的新 client（全败 None）。

    probe_all=False（401 热切换）：命中第一个就停（省请求）；
    probe_all=True（冒烟诊断）：把所有候选都探一遍，**每个候选一行掩码结果日志**，
      便于托管日志里一眼看出哪些 key 通/不通；最终仍按候选优先级选第一个可用的。
    命中即更新 CFG.llm_api_key 并设 _CURRENT_CLIENT，本进程后续调用全走它。
    """
    global _DISCOVERY_DONE, _CURRENT_CLIENT
    cands = _key_candidates(exclude)
    winner: tuple[str, str] | None = None
    for label, key in cands:
        try:
            probe = _client_with_key(key, timeout=min(CFG.request_timeout, 30))
            probe.chat.completions.create(
                model=CFG.model,
                messages=[{"role": "user", "content": "回复两个字：正常"}],
                max_tokens=512,  # 推理模型需预留思考 token，太小会 400 造成假阴性
            )
            log(f"gateway key probe [{cause}]: {label} -> OK")
            rec().event("gateway_key_probe", {"cause": cause, "candidate": label,
                                              "result": "ok"})
            if winner is None:
                winner = (label, key)
                if not probe_all:
                    break
        except Exception as e:
            status = getattr(e, "status_code", None)
            result = f"rejected({status})" if status else f"failed({type(e).__name__})"
            log(f"gateway key probe [{cause}]: {label} -> {result}")
            rec().event("gateway_key_probe", {"cause": cause, "candidate": label,
                                              "result": result})
    if winner is None:
        log(f"gateway key probe [{cause}]: ALL {len(cands)} candidates failed — "
            f"网关拒绝了全部 key/匿名（托管需注入真实 DEEPSEEK_API_KEY）")
        return None
    label, key = winner
    CFG.llm_api_key = key
    _DISCOVERY_DONE = True
    _CURRENT_CLIENT = make_client()
    log(f"gateway key selected [{cause}]: {label} — subsequent calls use it")
    return _CURRENT_CLIENT


def _try_reauth(e: BaseException) -> OpenAI | None:
    """网关 401（key 被拒）时按候选清单重探测（排除刚失败的 key），命中则热切换。

    实测 2026-10-15：本届转写网关把 Authorization 透传给上游 DeepSeek，内置
    "tsec-bench" 被上游 401（"Your api key: ****ench is invalid"）。
    命中即更新 CFG.llm_api_key/_CURRENT_CLIENT，全局生效；只试一次防死循环。
    """
    global _REAUTH_TRIED
    if _REAUTH_TRIED or getattr(e, "status_code", None) != 401:
        return None
    _REAUTH_TRIED = True
    return discover_gateway_key("reauth", exclude={CFG.llm_api_key})


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
    global _CURRENT_CLIENT
    if _CURRENT_CLIENT is not None:
        client = _CURRENT_CLIENT  # 其他线程已热切换过 key，本调用跟随
    while True:
        if STOP.is_set():
            raise RuntimeError("stopped: platform ended")
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
            # 401 热切换：转写网关透传 key 给上游，内置 key 被拒时按候选 key 试并重试本轮
            new_client = _try_reauth(e)
            if new_client is not None:
                client = _CURRENT_CLIENT = new_client
                continue
            # 4xx 认证/参数错误（除 429 限流）重试无意义 → 立即失败
            status = getattr(e, "status_code", None)
            if status is not None and 400 <= status < 500 and status != 429:
                if status == 401 and CFG.mode == "hosted":
                    log("LLM fatal auth error (401): 转写网关拒绝了所有候选 key——"
                        "托管模式需在平台环境变量 DEEPSEEK_API_KEY 注入**真实 DeepSeek key**"
                        "（内置 tsec-bench 是上届约定，本届网关透传上游鉴权，会被 401）")
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
        # 复用 bench.api：自动分类 TaskFinishedError（already finished 终态）
        # 并带网络重试；业务错误返回 None 走重试
        try:
            return bench.fetch_challenges()
        except TaskFinishedError:
            raise
        except Exception:
            return None

    def _watch():
        prev: dict = {}
        first = True
        while not STOP.is_set():
            try:
                items = _fetch()
            except Exception as e:
                if isinstance(e, TaskFinishedError):
                    platform_ended("watcher", e)
                    return
                rec().event("platform_poll_error", {"error": f"{type(e).__name__}: {e}"})
                time.sleep(interval)
                continue
            if items is None:
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
    """验证：网关连通、模型可用、shell 工具可用、VPN 预检、平台题库可达。"""
    ok = True

    # 1. 模型网关（hosted：先跑 key 候选发现——匿名/显式/平台token/真实key 逐个探测，
    #    每个候选一行掩码结果日志；命中者即本会话使用的 key。local：直连官方 API，
    #    失败时 401 热切换兜底）
    if CFG.mode == "hosted":
        nc = discover_gateway_key("smoke", probe_all=True)
        if nc is not None:
            client = nc
            log(f"model gateway OK (key selected; see key probe lines above), "
                f"active key={_key_mask(CFG.llm_api_key)}")
        else:
            log("model gateway FAILED: 网关拒绝了全部 key 候选——托管需在平台环境变量 "
                "DEEPSEEK_API_KEY 注入**真实 DeepSeek key**（内置 tsec-bench 是上届约定，"
                "本届转写网关透传上游鉴权；匿名也被拒说明网关强制鉴权）")
            ok = False
    else:
        try:
            r = client.chat.completions.create(
                model=CFG.model,
                messages=[{"role": "user", "content": "回复两个字：正常"}],
                max_tokens=512,  # 推理模型（deepseek-flash）需预留思考 token
            )
            log(f"model gateway OK: {r.choices[0].message.content!r}")
        except Exception as e:
            # 401 热切换：按候选清单重探测（排除当前 key）并重试
            nc = _try_reauth(e)
            if nc is not None:
                try:
                    r = nc.chat.completions.create(
                        model=CFG.model,
                        messages=[{"role": "user", "content": "回复两个字：正常"}],
                        max_tokens=512,
                    )
                    log(f"model gateway OK (after key switch): "
                        f"{r.choices[0].message.content!r}")
                except Exception as e2:
                    log(f"model gateway FAILED (after key switch): {type(e2).__name__}: {e2}")
                    ok = False
            else:
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

    # 4. 平台题库可达性：任何 HTTP 响应（含 4xx 业务错误）都算网络可达；
    #    只有连接/DNS/超时失败才是"题库没访问"。曾因冒烟不测平台 API，
    #    hosted 锁网漏放行平台域名时容器看似正常、实则调度器无限重试不出分。
    if CFG.benchmark_token:
        try:
            items = bench.fetch_challenges()
            log(f"platform API OK: {len(items)} challenges visible")
        except bench.TaskFinishedError:
            log("platform API reachable (task already finished)")
        except bench.PlatformError as e:
            # 拿到业务错误（token 失效/状态不对）= 网络层可达，冒烟放行，正式运行再按错误码处置
            log(f"platform API reachable (business error at smoke, non-fatal): {e}")
        except Exception as e:
            if CFG.mode == "hosted":
                log(f"platform API UNREACHABLE (hosted, fatal): {type(e).__name__}: {e}")
                ok = False
            else:
                log(f"platform API unreachable (local mode, non-fatal for smoke): "
                    f"{type(e).__name__}: {e}")
    elif CFG.mode == "hosted":
        log("BENCHMARK_TOKEN empty in hosted mode — platform should inject it; "
            "题库请求将全部 401/404，检查平台注入")
        ok = False
    else:
        log("BENCHMARK_TOKEN empty (local mode, non-fatal for smoke)")

    return 0 if ok else 1


def _is_context_overflow(e: BaseException) -> bool:
    """LLM 上下文超限判定：这类错误重试/续跑都无意义，只能终止本题。"""
    text = f"{type(e).__name__}: {e}".lower()
    return any(k in text for k in (
        "context_length_exceeded", "context_length", "context window",
        "maximum context length", "maximum context window",
        "too many tokens", "reduce the length", "input length and `max_tokens`",
        "string too long", "prompt is too long",
    ))


def _with_fix_note(res, fix: str):
    """给修复后的工具结果加一行说明；结果是图片 parts 时改写首个 text 段。"""
    note = f"[参数已自动修复：{fix}] "
    if isinstance(res, list):
        for part in res:
            if isinstance(part, dict) and part.get("type") == "text":
                part["text"] = note + part["text"]
                return res
        return [{"type": "text", "text": note.rstrip()}] + list(res)
    return note + str(res)


def _strip_images(messages: list) -> int:
    """把消息内容里的图片 part 剥掉（网关不支持图像时降级），返回移除的图片数。"""
    removed = 0
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        keep = []
        had = False
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                removed += 1
                had = True
                continue
            keep.append(part)
        if had:
            keep.insert(0, {"type": "text",
                            "text": "[网关不支持图像输入：本条图片已移除，请改用上一条文本里的"
                                    "OCR/元数据，或用 shell 分析原文件]"})
            m["content"] = keep
    return removed


def solve_task(client: OpenAI, code: str, task_card: str, budget: int, conv,
               *, messages: list | None = None,
               start_turn: int = 0) -> tuple[str, str, int, list]:
    """单题分段解题：本段直到模型输出最终总结（无工具调用）或本段轮次预算到点。

    返回 (status, last_content, turns_used, messages)：
      done            模型输出无工具总结（本段结束，是否通关由控制器查平台判定）
      paused          本段轮次预算到点 → 控制器关闭题目释放名额、保留上下文，稍后续跑
      stopped         平台终态（STOP 置位）
      vpn_abandon     VPN 长时间未恢复，放弃本题
      deadline        全局任务时限耗尽
      llm_error       LLM 调用致命错误
      context_overflow 上下文超出模型窗口（本题不可再续跑）
    messages/start_turn：续跑时传入重建好的上下文与轮次偏移，此时不再写 system/user
    （system/user 已在首段落盘，续跑只追加续跑通知）。
    对话全量、忠实落盘（conversation.jsonl）：发送什么、输出什么，原样记录不裁剪。
    """
    if messages is None:
        messages: list = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task_card},
        ]
        # 忠实记录本题输入（system + 任务卡）
        conv.write("system", turn=f"{code}:0", content=SYSTEM_PROMPT)
        conv.write("user", turn=f"{code}:0", content=task_card)
    last_content = ""
    turns_used = 0
    turn = 0
    while turn < budget:
        if STOP.is_set():
            return "stopped", last_content, turns_used, messages
        # VPN 守卫：VPN 挂起 → 本题挂起等待恢复（不烧轮次预算），恢复后原地继续。
        # 等待超过 AGENT_VPN_WAIT_MAX（默认 600s）仍未恢复才放弃本题。
        if VPN_DOWN.is_set():
            wait_max = float(os.environ.get("AGENT_VPN_WAIT_MAX", "600"))
            t0 = time.time()
            conv.write("note", turn=f"{code}:{turn}",
                       content=f"VPN down detected; suspending task (max {wait_max:.0f}s)")
            log(f"[{code}] VPN down; task suspended (waiting for recovery)")
            while VPN_DOWN.is_set() and not STOP.is_set() and time.time() - t0 < wait_max:
                time.sleep(3)
            waited = time.time() - t0
            if VPN_DOWN.is_set():
                conv.write("error", turn=f"{code}:{turn}",
                           error=f"VPN still down after {waited:.0f}s; abandoning task")
                log(f"[{code}] VPN still down after {waited:.0f}s; abandoning")
                return "vpn_abandon", last_content, turns_used, messages
            conv.write("note", turn=f"{code}:{turn}",
                       content=f"VPN recovered after {waited:.0f}s; resuming")
            log(f"[{code}] VPN recovered after {waited:.0f}s; resuming task")
            continue  # 恢复后从当前 turn 继续（不烧预算）
        if (rem := remaining_sec()) is not None and rem <= 0:
            return "deadline", last_content, turns_used, messages
        turn += 1
        t_llm_start = time.time()
        resp = None
        try:
            resp = call_llm(client, messages)
        except Exception as e:
            # 多模态降级：网关拒收图片 → 剥掉 image part 改发纯文本，本会话后续不再附图
            dropped = _strip_images(messages) if CFG.vision_ok is not False else 0
            if dropped:
                CFG.vision_ok = False
                log(f"[{code}] gateway rejected image input ({type(e).__name__}: {e}); "
                    f"dropped {dropped} image(s), retrying text-only")
                conv.write("note", turn=f"{code}:{turn}",
                           content=f"网关拒图，降级为文本模式：{type(e).__name__}: {e}")
                rec().event("vision_degraded", {"code": code, "dropped": dropped,
                                                "error": f"{type(e).__name__}: {e}"[:400]})
                try:
                    resp = call_llm(client, messages)
                except Exception as e2:
                    e = e2
            if resp is None:
                log(f"[{code}] fatal LLM error: {type(e).__name__}: {e}")
                conv.write("error", turn=f"{code}:{turn}", error=f"{type(e).__name__}: {e}",
                           started_at=t_llm_start)
                status = "context_overflow" if _is_context_overflow(e) else "llm_error"
                return status, last_content, turns_used, messages
        turns_used += 1
        t_llm_end = time.time()

        msg = resp.choices[0].message
        tlabel = f"{code}:{start_turn + turn}"
        tool_calls_raw = getattr(msg, "tool_calls", None) or []
        # AI 输出全量落盘：thinking / output / tool_calls 原文
        # started_at/ended_at = 本次 LLM 调用起止（供甘特图）
        conv.write(
            "assistant",
            turn=tlabel,
            thinking=getattr(msg, "reasoning_content", None),
            output=msg.content,
            tool_calls=[{"id": tc.id, "name": tc.function.name,
                         "arguments": tc.function.arguments} for tc in tool_calls_raw],
            usage=getattr(resp, "usage", None),
            started_at=t_llm_start,
            ended_at=t_llm_end,
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
            # 本题上下文的最终总结 → 交还控制器（是否通关由控制器查平台）
            return "done", msg.content or "", turns_used, messages

        for tc in tool_calls_raw:
            args: dict = {}
            raw_args = tc.function.arguments or "{}"
            t_tool_start = time.time()
            if tc.function.name == "shell":
                parsed, fix = parse_tool_arguments(raw_args)
                if parsed is None:
                    result = f"error: {fix or 'invalid tool arguments'}"
                else:
                    args = parsed
                    if fix:
                        # 模型把参数序列化坏了（action 混入 XML 片段/整段命令/JSON 漏引号）：
                        # 直接修复执行，避免整轮作废；原样记录在 assistant.tool_calls 里
                        log(f"[{code}] tool args repaired: {fix}")
                        rec().event("tool_arg_repaired", {"code": code,
                                                          "tool_call_id": tc.id,
                                                          "fix": fix,
                                                          "raw": raw_args[:400]})
                        result = _with_fix_note(dispatch_shell(args), fix)
                    else:
                        result = dispatch_shell(args)
            else:
                result = f"error: unknown tool {tc.function.name!r}"
            t_tool_end = time.time()
            # 工具结果全量落盘（与 assistant 条目通过 tool_call_id 关联）
            # started_at/ended_at = 工具执行起止（供甘特图）
            conv.write("tool", turn=tlabel, tool_call_id=tc.id, tool=tc.function.name,
                       arguments=args, result=result,
                       started_at=t_tool_start, ended_at=t_tool_end)
            # 结果可能是 str，也可能是多模态 parts 列表（action=view）
            _first = (result if isinstance(result, str) else
                      next((p.get("text", "") for p in result
                            if isinstance(p, dict) and p.get("type") == "text"), ""))
            log(f"tool#{tc.id[-6:]} {tc.function.name} -> "
                f"{_first.splitlines()[0][:160] if _first else ''}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    log(f"[{code}] segment turn budget ({budget}) reached "
        f"(total {start_turn + turn} turns) — pausing, context kept")
    return "paused", last_content, turns_used, messages


def closing_summary(client: OpenAI, code: str, messages: list, conv) -> str:
    """通关后复盘：在同一题上下文里追加一条总结请求（无工具），回复作为普通消息
    与其它对话一样忠实写入 conversation.jsonl。返回总结文本。"""
    user_msg = {"role": "user", "content": CLOSING_SUMMARY_PROMPT}
    messages.append(user_msg)
    conv.write("user", turn=f"{code}:closing", content=CLOSING_SUMMARY_PROMPT)
    t_llm_start = time.time()
    try:
        resp = call_llm(client, messages, use_tools=False)
    except Exception as e:
        log(f"[{code}] closing summary LLM error: {type(e).__name__}: {e}")
        conv.write("error", turn=f"{code}:closing", error=f"{type(e).__name__}: {e}",
                   started_at=t_llm_start)
        return ""
    t_llm_end = time.time()
    msg = resp.choices[0].message
    assistant_msg = {"role": "assistant", "content": msg.content}
    messages.append(assistant_msg)
    conv.write("assistant", turn=f"{code}:closing",
               thinking=getattr(msg, "reasoning_content", None),
               output=msg.content,
               kind_note="closing_summary",   # 标记本条为通关复盘
               usage=getattr(resp, "usage", None),
               started_at=t_llm_start,
               ended_at=t_llm_end)
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
    STOP.clear()  # 进程复用场景下重置终态标记
    # 只解部分题目（测试用）：优先级 --task/AGENT_TASK_FILTER > AGENT_TASKS > 配置默认
    raw_sel = (os.environ.get("AGENT_TASK_FILTER", "").strip()
               or os.environ.get("AGENT_TASKS", "").strip() or CFG.tasks)
    task_filter = select_tasks(raw_sel)
    if task_filter:
        log(f"task selection: {', '.join(task_filter)}（其余题目跳过）")
    else:
        log("task selection: 全部题目")
    R.event("run_start", {"mode": CFG.mode, "model": CFG.model,
                          "max_turns": CFG.max_turns, "deadline_sec": CFG.deadline_sec,
                          "task_filter": task_filter})
    # task_filter 落盘：panel 用它把平台统计（题数/flag 数/总分）限定到选题范围
    R.run_start(mode=CFG.mode, model=CFG.model, max_turns=CFG.max_turns,
                task_filter=task_filter)
    log(f"records dir: {R.dir}")
    start_platform_watcher()
    task_budget = CFG.task_max_turns
    results: dict[str, str] = {}   # code -> outcome(pass/fail)；暂停题不写（保留续跑）
    attempts: dict[str, int] = {}
    total_turns = 0

    def snapshot() -> list | None:
        try:
            return bench.fetch_challenges()
        except TaskFinishedError as e:
            platform_ended("snapshot", e, results)
            return None
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
    if STOP.is_set():
        # platform_ended 已写过 run_finish，这里只补最终汇总事件
        rec().event("final_summary", {"note": "after platform ended", "results": results})
    else:
        rec().event("run_finish", {"reason": "controller_done", "results": results})
        rec().run_finish(reason="controller_done", results=results)
    return 0


def select_tasks(raw: str) -> list[str]:
    """AGENT_TASKS / AGENT_TASK_FILTER → 模式列表（逗号或空白分隔，支持 * ? 通配）。

    空 → 空列表（= 不过滤，做全部题）。
    """
    return [p for p in (raw or "").replace(",", " ").split() if p]


def task_selected(code: str, patterns: list) -> bool:
    """题目编号是否在选择范围内；patterns 为空表示全部选中。"""
    if not patterns:
        return True
    return any(fnmatch.fnmatchcase(code, p) for p in patterns)


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
      results/attempts/failures/resume_meta 由锁保护；平台 start/close 由调度线程串行执行。
    - 分段解题：每题每段 task_budget 轮，到点不判死——关闭题目释放名额、保留全量上下文
      （resume_meta），等没有新题可派时再 start 续跑（重启靶机 + 追加续跑通知）。
      只有上下文超模型窗口 / 连续收尾不通关达上限 / 真失败达 max_failures 才终止本题。
    - 调度线程绝不因单个题的异常崩溃（worker 异常记 fail 继续）。
    """
    lock = threading.Lock()
    running: dict[str, threading.Thread] = {}
    cap = int(os.environ.get("AGENT_MAX_PARALLEL", "5"))
    checked_selection = False  # 首次快照时核对一次题目选择是否命中（打错字要出声）

    # VPN 守卫：auto=模式感知（hosted 强制；local 模式只在 VPN 曾经通时启用，不伤裸机开发），
    # 1/0 显式开关。探测连续失败 N 次 → VPN_DOWN 置位：暂停派发新题（不消耗 attempts），
    # worker 挂起等待；恢复成功即解除。探测间隔 20s，只在调度线程跑。
    vpn_mode = os.environ.get("VPN_GUARD", "auto").lower()
    if vpn_mode == "auto":
        vpn_mode = "1" if CFG.mode == "hosted" else "0"
    vpn_probe_fail = 0
    vpn_guard_on = vpn_mode in ("1", "on", "true")
    last_vpn_probe = 0.0
    VPN_PROBE_INTERVAL = float(os.environ.get("VPN_PROBE_INTERVAL", "20"))
    if vpn_guard_on:
        rec().event("vpn_guard_enabled", {"mode": CFG.mode, "vpn_mode": vpn_mode})

    # 分段解题状态（调度线程与 worker 共享，受 lock 保护）：
    #   failures:  真失败次数（LLM 致命错/上下文超限/VPN 放弃/时限），达上限才判死
    #   resume_meta: 暂停题的续跑状态 {pauses, gives, target, turns}
    failures: dict[str, int] = {}
    resume_meta: dict[str, dict] = {}

    def _slot_waiters(items: list | None, self_code: str) -> bool:
        """是否**还有别的题在等这个并发名额**（新题或待续跑的题）。

        段到点的取舍：有人等 → 照旧关题让位（随后可 start 续跑，代价是靶机重启）；
        没人等 → worker 在**原容器、原上下文**里继续跑，避免只剩几题时的无谓重启
        （重启会让靶机状态重置、flag 变值、内网地址漂移）。
        快照拿不到时保守返回 True，维持原行为。
        """
        if not items:
            return True
        for c in items:
            cc = c.get("unique_code")
            if cc == self_code or c.get("is_completed"):
                continue
            if not task_selected(cc, task_filter):
                continue
            with lock:
                if cc in running or results.get(cc) == "pass":
                    continue
                if failures.get(cc, 0) >= max(CFG.max_failures, 1):
                    continue
            return True
        return False

    def worker(task: dict, addr: str, attempt: int, resume: bool = False) -> None:
        code = task["unique_code"]
        set_task_cwd(_task_workdir(code))
        last = ""
        state = None
        closing_text = ""
        status = "llm_error"
        outcome = "fail"
        conv = rec().conversation(code)
        messages: list | None = None
        meta = resume_meta.get(code) or {}
        pauses = int(meta.get("pauses", 0))
        gives = int(meta.get("gives", 0))
        old_target = meta.get("target", "")
        start_turn = 0
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

            if resume:
                # 续跑：从 conversation.jsonl 重建全量上下文（记录即事实），
                # 再追加「靶机已重启」的续跑通知；不重写 system/user。
                loaded = rec().load_messages(code)
                if loaded:
                    messages = loaded
                    start_turn = sum(1 for m in messages if m["role"] == "assistant")
                    note = (GIVEUP_NOTE if meta.get("reason") == "giveup" else RESUME_NOTE).format(
                        new_target=addr, old_target=old_target or "（未知）",
                        workdir=get_task_cwd() or _task_workdir(code),
                        budget=budget, turns=start_turn)
                    messages.append({"role": "user", "content": note})
                    conv.write("user", turn=f"{code}:{start_turn}", content=note)
                    log(f"[{code}] resume segment: {start_turn} prior turns restored, "
                        f"target={addr} (was {old_target or '?'})")
                else:
                    log(f"[{code}] resume context unavailable; starting fresh segment")

            while True:
                status, last, used, messages = solve_task(
                    client, code, card, budget, conv, messages=messages, start_turn=start_turn)

                # 校验平台状态（快照失败重试，避免误判）
                for _try in range(3):
                    items_now = snapshot()
                    if items_now is not None:
                        state = bench.challenge_state(items_now, code)
                        if state is not None:
                            break
                    time.sleep(4)
                completed = bool(state and state["is_completed"])

                # 段到点但**没人等这个名额**（无新题、也无待续跑的题）→ 不关题不重启：
                # 原容器、原上下文直接续一段，省掉靶机重置（状态/flag 值/内网地址都会变）。
                if status == "paused" and not completed and not _slot_waiters(items_now, code):
                    start_turn += used
                    budget = task_budget
                    if rem := remaining_sec():
                        budget = max(10, min(task_budget, int(rem // 6)))
                    rec().event("segment_continue", {"code": code, "turns": start_turn,
                                                      "target": addr})
                    log(f"[{code}] turn budget hit but nobody waits for this slot → "
                        f"continue in place ({start_turn} turns so far, no close/restart)")
                    continue
                break

            # 通关后：同一上下文直接追加复盘请求，总结像普通消息一样落 conversation.jsonl
            if completed and messages is not None:
                closing_text = closing_summary(client, code, messages, conv)

            if completed:
                outcome = "pass"
                with lock:
                    resume_meta.pop(code, None)
            elif status in ("llm_error", "context_overflow", "vpn_abandon", "deadline"):
                # 真失败：上下文不保留，计入失败次数，达上限后不再派发
                with lock:
                    failures[code] = (max(CFG.max_failures, 1) if status == "context_overflow"
                                      else failures.get(code, 0) + 1)
                    resume_meta.pop(code, None)
                outcome = "fail"
                rec().event("task_abandoned", {"code": code, "status": status,
                                               "failures": failures.get(code, 0)})
                log(f"[{code}] abandoned ({status}); failures={failures.get(code, 0)}"
                    f"{' (context overflow — task stopped)' if status == 'context_overflow' else ''}")
            elif status == "stopped":
                outcome = "fail"  # 平台终态，见 platform_ended
                with lock:
                    resume_meta.pop(code, None)
            else:
                # paused（本段轮次到点）或 done 但未通关（模型提前收尾）→
                # 关闭题目释放名额，保留上下文排到队尾，等没有新题时再续跑。
                giveup = status == "done"
                gives = gives + 1 if giveup else 0
                pauses += 1
                too_many = ((CFG.giveup_max > 0 and gives >= CFG.giveup_max) or
                            (CFG.pause_max > 0 and pauses > CFG.pause_max))
                if too_many:
                    with lock:
                        # 终止式放弃：直接置满失败上限，不再被重新派发
                        # （否则会被当成新题从头再跑，白白烧轮次）
                        failures[code] = max(CFG.max_failures, 1)
                        resume_meta.pop(code, None)
                    outcome = "fail"
                    rec().event("task_abandoned", {
                        "code": code,
                        "status": "giveup_loop" if giveup else "pause_limit",
                        "pauses": pauses, "gives": gives})
                    log(f"[{code}] abandoning after {pauses} segments "
                        f"({'consecutive give-ups' if giveup else 'pause limit'})")
                else:
                    outcome = "paused"
                    with lock:
                        resume_meta[code] = {"pauses": pauses, "gives": gives,
                                             "target": addr, "turns": start_turn + used,
                                             "reason": "giveup" if giveup else "paused"}
                    rec().event("task_paused", {
                        "code": code, "pauses": pauses, "gives": gives,
                        "reason": "giveup" if giveup else "turn_budget",
                        "turns": start_turn + used, "target": addr})
                    log(f"[{code}] segment done ({status}); pausing & releasing slot "
                        f"(segment {pauses}, {start_turn + used} total turns)")
        except Exception as e:
            log(f"[{code}] worker error: {type(e).__name__}: {e}")
            rec().event("task_error", {"code": code, "error": f"{type(e).__name__}: {e}"})
            outcome = "fail"
        finally:
            messages = None  # 该题上下文交还给 resume_meta/记录（暂停时已存），随 GC 回收
            set_task_cwd("")  # 线程结束清 thread-local
            closed_at = time.strftime("%Y-%m-%d %H:%M:%S")
            completed = bool(state and state["is_completed"])
            if outcome == "pass" and not completed:
                outcome = "fail"  # 快照异常兜底：平台未确认通关不算 pass
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
            if outcome != "paused":
                # 暂停不算结果：题目保留，排到队尾等续跑
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
        if STOP.is_set():
            log("platform ended; scheduler stopping")
            break
        if (rem := remaining_sec()) is not None and rem <= 60:
            log("deadline near; stopping scheduler")
            break

        # --- VPN 运行时守卫：周期（默认 20s）探测，连续失败 → 置位暂停派发；成功即恢复 ---
        if vpn_guard_on and not STOP.is_set() and time.time() - last_vpn_probe >= VPN_PROBE_INTERVAL:
            last_vpn_probe = time.time()
            if _vpn_ok(timeout=5):
                vpn_probe_fail = 0
                if VPN_DOWN.is_set():
                    VPN_DOWN.clear()
                    rec().event("vpn_recovered", {})
                    log("VPN probe OK — resume dispatching new tasks")
            else:
                vpn_probe_fail += 1
                if vpn_probe_fail >= 3 and not VPN_DOWN.is_set():
                    VPN_DOWN.set()
                    rec().event("vpn_down", {"consecutive_fails": vpn_probe_fail})
                    log(f"VPN DOWN ({vpn_probe_fail} consecutive probe failures) — "
                        f"pausing dispatch; running workers will wait")

        # 回收已完成 worker
        with lock:
            for code in [c for c, t in running.items() if not t.is_alive()]:
                running.pop(code, None)

        items = snapshot()
        if items is None:
            # 平台终态 → 直接收尾；否则短暂等待重试
            if STOP.is_set():
                break
            time.sleep(8)
            continue
        for code in bench.release_completed(items):
            rec().event("challenge_change", {"code": code, "field": "controller_close",
                                             "old": "available", "new": "closed",
                                             "note": "released after completion"})
            with lock:
                results.setdefault(code, "pass")
        items = snapshot() or items

        # 题目选择校验：只跑部分题时，打错字不吭声很难查 → 首次快照核对一次
        if task_filter and not checked_selection:
            checked_selection = True
            matched = sorted(c["unique_code"] for c in items
                             if task_selected(c["unique_code"], task_filter))
            if matched:
                log(f"task selection matched {len(matched)}: {', '.join(matched)}")
            else:
                log(f"WARNING: task selection matched nothing: {task_filter} "
                    f"(platform has {len(items)} tasks)")
                rec().event("task_selection_empty",
                            {"patterns": task_filter, "total": len(items)})

        # 候选：未通关、不在跑、未超真失败上限、在题目选择范围内
        def eligible(c: dict) -> bool:
            if c["is_completed"]:
                return False
            with lock:
                if c["unique_code"] in running:
                    return False
                if results.get(c["unique_code"]) == "pass":
                    return False
                if failures.get(c["unique_code"], 0) >= max(CFG.max_failures, 1):
                    return False
            return task_selected(c["unique_code"], task_filter)

        def _key(c: dict) -> tuple:
            return (0 if c["container_status"] == "available" else 1,
                    bench.DIFF_RANK.get(c["difficulty"], 9),
                    c.get("level", 9), c["unique_code"])

        fresh = sorted([c for c in items if eligible(c) and c["unique_code"] not in resume_meta],
                       key=_key)
        resumable = sorted([c for c in items if eligible(c) and c["unique_code"] in resume_meta],
                           key=lambda c: (resume_meta.get(c["unique_code"], {}).get("pauses", 0),
                                          c["unique_code"]))
        # 分段策略：暂停题只在没有新题可派时才续跑——名额与轮次先留给还没跑的题（含简单题）
        candidates = fresh or resumable

        if not candidates and not running:
            break  # 全部完成或全部结束（真失败达上限）

        if VPN_DOWN.is_set():
            # 暂停派发（平台 API 走公网不依赖 VPN，snapshot/终态检测/释放照常；
            # 只有靶场渗透需要 VPN）：不 start、不消耗 attempts，等探测恢复后放行
            time.sleep(10)
            continue

        # 扩容：直到槽位用尽或平台拒绝（max active）
        for task in candidates:
            with lock:
                if len(running) >= cap:
                    break
            code = task["unique_code"]
            addr = _task_target(task)
            # 续跑必须重新 start：题目容器要重启（释放名额期间已被关闭），
            # 以便拿到新 target 并在续跑通知里如实告知模型「靶机已重置」
            if task["container_status"] != "available" or not addr or code in resume_meta:
                started = False
                slot_full = False
                for _try in range(3):
                    if STOP.is_set():
                        break
                    try:
                        resp = bench.start_challenge(code)
                        addr = _task_target(resp) or addr
                        started = True
                        break
                    except TaskFinishedError as e:
                        platform_ended("start", e, results)
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
                            # 任务侧状态问题：记 fail，不重试死循环
                            log(f"[{code}] start invalid_state: {e.message}")
                            break
                        elif e.code == "duplicate":
                            # 幂等：容器已在跑，直接复用现有地址
                            started = True
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
                if STOP.is_set():
                    break  # 平台终态 → 退出候选循环，外层收尾
                if slot_full:
                    break  # 名额满了，等 worker 完成后再扩
                if not started:
                    # start 失败计入真失败上限，避免同一题无限重试死循环
                    with lock:
                        attempts[code] = attempts.get(code, 0) + 1
                        failures[code] = failures.get(code, 0) + 1
                        if failures[code] >= max(CFG.max_failures, 1):
                            results[code] = "fail"
                    continue
            if not addr:
                log(f"[{code}] no container_addr from start")
                with lock:
                    attempts[code] = attempts.get(code, 0) + 1
                    failures[code] = failures.get(code, 0) + 1
                    results[code] = "fail"
                continue

            with lock:
                attempts[code] = attempts.get(code, 0) + 1
                attempt_no = attempts[code]
                is_resume = code in resume_meta
            # 题目生命周期：启动时间（task_start，对应 task_close 的 closed_at/duration）
            rec().task_event(code, "task_start",
                             started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                             target=addr, attempt=attempt_no,
                             resume=is_resume,
                             parallel=len(running) + 1,
                             flags=f"0/{task['flag_count']}")
            TASK_START_TS[code] = time.time()
            log(f"=== task {code} attempt {attempt_no} target={addr} "
                f"{'(resume segment) ' if is_resume else ''}"
                f"[parallel {len(running) + 1}/{cap}] ===")
            t = threading.Thread(target=worker, args=(task, addr, attempt_no, is_resume),
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

    # 运行终止（平台终态/时限）时仍有暂停中的题：补记 fail，
    # 保证 results/run_finish 完整（暂停态本身已落 task_close{outcome:paused}）
    if resume_meta:
        with lock:
            for code in list(resume_meta):
                results.setdefault(code, "fail")
                rec().event("task_abandoned", {"code": code, "status": "run_ended",
                                               "pauses": resume_meta[code].get("pauses", 0)})
                log(f"[{code}] still paused at shutdown; recorded as fail")
            resume_meta.clear()

    # 平台终态：所有 agent（worker 线程）已停，尽力关闭其靶场容器释放资源
    #（平台已截止时 close 可能返回 invalid_state——忽略即可，平台侧会自动回收）
    if STOP.is_set():
        for code in list(attempts.keys()):
            try:
                bench.close_challenge(code)
                rec().event("challenge_change", {"code": code, "field": "controller_close",
                                                 "old": "available", "new": "closed",
                                                 "note": "platform ended cleanup"})
            except Exception:
                pass
        log("all workers stopped; active challenge containers cleanup attempted")


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
    ap.add_argument("--task", default=None, metavar="CODE[,CODE...]",
                    help="只解指定题目（调试用）：逗号分隔、支持 * ? 通配，"
                         "如 --task e1-01 / --task b-0*,c-02")
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
