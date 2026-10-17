"""Solver 契约原语共享层（从 main.py 抽取，dual_main.py / main.py 共用）。

单一真值源：所有契约状态（ORCH_MODE / TASK_PROMPT / _reported / TOTALS …）
都住在本模块；main.py 通过模块级 __getattr__ 转发访问（test_solver_contract.py
与 pi_main.py 的 `mod.XXX` 访问形态保持不变，且 setup_orch_mode 的变更对
两个消费方立即可见）。

契约本体见 architecture.md §2.1-2.4：
  读 SOLVER_TASK(task.json) → 干活 → out/flags/NNN.txt + out/result.json → exit 0。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, first '=' splits; env takes precedence."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(HERE / ".env")


def cfg(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


API_KEY = cfg("LLM_API_KEY") or cfg("DEEPSEEK_API_KEY")
BASE_URL = cfg("LLM_BASE_URL", "https://api.deepseek.com/v1")
MODEL = cfg("LLM_MODEL", "deepseek-chat")
MAX_TURNS = int(cfg("SOLVER_MAX_TURNS", "60"))
MAX_MINUTES = float(cfg("SOLVER_MAX_MINUTES", "40"))
CMD_TIMEOUT = int(cfg("SOLVER_CMD_TIMEOUT", "60"))
# 单条命令的 harness 硬顶：模型可以请求更长，但执行层不允许一条命令吃掉大半片。
CMD_HARD_MAX_S = int(cfg("SOLVER_CMD_MAX_S", "90"))
OUTPUT_LIMIT = int(cfg("SOLVER_OUTPUT_LIMIT", "12000"))
# 上下文裁剪：DeepSeek 128k 上下文下 45k 过早；长利用链需要更多历史。
TRIM_TOKENS = int(cfg("SOLVER_TRIM_TOKENS", "90000"))
TRIM_KEEP_MSGS = int(cfg("SOLVER_TRIM_KEEP_MSGS", "60"))
# 连续「只说不做」的停机阈值：3 太早，实测会把已拿到 LFI/凭据的链子掐断。
MAX_PLAIN_NUDGES = int(cfg("SOLVER_MAX_PLAIN_NUDGES", "6"))
# 收敛守卫：连续多少轮没有新资产/凭据/端点就提示换面；多少轮无新事实直接收尾。
NO_FACT_NUDGE_TURNS = int(cfg("SOLVER_NO_FACT_NUDGE_TURNS", "8"))
STALE_HARD_TURNS = int(cfg("SOLVER_STALE_HARD_TURNS", "30"))
MAX_NO_FACT_NUDGES = int(cfg("SOLVER_MAX_NO_FACT_NUDGES", "2"))
MAX_TURNS_NO_FLAG = int(cfg("SOLVER_MAX_TURNS_NO_FLAG", "45"))


def effective_command_timeout(
    requested: int, remaining: float | None, *, hard_max: int = CMD_HARD_MAX_S
) -> int:
    """单条命令墙钟上限 = min(请求值, 剩余片的一半, harness 硬顶)。"""
    t = max(5, int(requested))
    if remaining is not None:
        t = min(t, max(10, int(remaining * 0.5)))
    return min(t, hard_max)


TASK_PROMPT = cfg("BOXPWNR_PROMPT", "") or ""
TARGET_INFO = cfg("BOXPWNR_TARGET_IP", "") or ""
CONTAINER = cfg("BOXPWNR_CONTAINER", "") or ""

# 本题时间片截止（Unix 秒）。编排器模式由 SOLVER_DEADLINE_EPOCH 注入；
# 非编排器模式为 None（无限预算）。用来给单条命令设上限、并在收尾阶段提示收敛。
DEADLINE_EPOCH: float | None = None
# 本题时间片总长（秒），编排器经 SOLVER_BUDGET_HINT 注入；用来判断「时间过半」。
SLICE_S: float = float(cfg("SOLVER_BUDGET_HINT", "0") or 0)


def remaining_seconds() -> float | None:
    """距时间片截止的剩余秒数；无截止（本地调试）时返回 None。"""
    if DEADLINE_EPOCH is None:
        return None
    return max(0.0, DEADLINE_EPOCH - time.time())


# ---- 编排器模式（architecture.md §2.1/§2.2）----
# SOLVER_TASK 指向 task.json 且没有 BOXPWNR_* 时，走编排器契约：
# 读 task.json → 解题 → 候选写 out/flags/*.txt + out/result.json。
SOLVER_TASK_PATH = cfg("SOLVER_TASK", "") or ""
ORCH_MODE = bool(SOLVER_TASK_PATH) and not TASK_PROMPT
_flag_seq = 0
TOTALS: dict = {}  # LLM 用量统计（write_result 收尾用）
TASK: dict = {}  # 当前 task.json（setup_orch_mode 写入）


def turn_limit() -> int:
    """轮数上限。编排器模式以时间片为唯一预算（deadline 由编排器强杀兜底），
    60 轮只是 BoxPwnr 直跑模式的保险丝 —— 难题常需要上百轮工具交互。"""
    return 10**9 if ORCH_MODE else MAX_TURNS


# ---------------------------------------------------------------- executor ---
def run_command(command: str, timeout: int) -> dict:
    """Run a shell command inside the challenge container (or locally).

    **一律用 `timeout -k 3 <t>` 包一层**（非容器模式也一样）。实测：solver 常写
    无超时的命令（如 `curl -s -i http://T/`），目标挂死时只有宿主侧 `t+20` 的
    粗杀兜底 —— 一条命令白烧 120s+ 时间片。内层 timeout 让超时精确到 t。

    外层仍 kill 整个进程组：内层 `timeout` 只终止直接子进程（bash），孙子进程
    （curl/nmap）会成孤儿，必须按组清。
    """
    inner = ["bash", "-lc", command]
    if CONTAINER:
        inner = ["docker", "exec", "-i", CONTAINER, *inner]
    argv = ["timeout", "-k", "3", str(timeout), *inner]

    started = time.time()
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return {
            "exit_code": 127,
            "output": f"executor error: {exc}",
            "timed_out": False,
            "seconds": 0.0,
        }

    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        timed_out = True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
    finally:
        # 无论正常结束还是超时都清一次组：正常路径下内层 timeout 可能只杀了 bash，
        # 孙子进程还挂在组里（孤儿）。
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    return {
        "exit_code": proc.returncode,
        "output": out or "",
        "timed_out": timed_out,
        "seconds": round(time.time() - started, 1),
    }


def truncate(text: str, limit: int = OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return text[:head] + f"\n...[truncated {len(text) - limit} chars]...\n" + text[-tail:]


# ------------------------------------------------------------- tool surface ---
def build_tool_specs() -> list[dict]:
    """模型可见的工具面（能力在 harness 里，不靠提示词叮嘱）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": (
                    "Run a shell command and return combined stdout/stderr. "
                    "Non-interactive commands only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "timeout_seconds": {
                            "type": "integer",
                            "description": (
                                f"Best-effort timeout (default {CMD_TIMEOUT}); "
                                f"the harness cap is {CMD_HARD_MAX_S}s."
                            ),
                        },
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recon",
                "description": (
                    "Deterministic recon returning compact JSON: mode=host runs a capped "
                    "nmap service scan; mode=web fetches headers/title/tech and runs a "
                    "time-capped, 404-filtered directory sweep. Prefer this over "
                    "hand-writing long nmap/gobuster pipelines."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["host", "web", "all"]},
                        "target": {"type": "string", "description": "host/IP for host/all"},
                        "url": {"type": "string", "description": "absolute URL for web/all"},
                    },
                    "required": ["mode"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "rea",
                "description": (
                    "Evidence-backed reverse engineering via REA + Ghidra on a LOCAL file. "
                    "mode=inspect returns an overview of a binary/firmware artifact; "
                    "mode=decompile returns pseudo-C for one procedure. Download the "
                    "binary to the workspace first, then call this."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["inspect", "decompile"]},
                        "path": {"type": "string", "description": "local file path"},
                        "procedure": {
                            "type": "string",
                            "description": "function name or address (mode=decompile)",
                        },
                    },
                    "required": ["mode", "path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "kb",
                "description": (
                    "Search the offline security knowledge base (payloads, CVE notes, "
                    "SOP checklists) and return the best matching snippets. Use it for "
                    "exact payload syntax / N-day techniques instead of guessing."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "service/CVE/technique keywords",
                        },
                        "top_k": {"type": "integer", "description": "default 3"},
                    },
                    "required": ["query"],
                },
            },
        },
    ]


def _kb_query(text: str, fingerprint: str = "") -> str:
    """题面 + 指纹 + 产品名扩展（Dify/HugeGraph/S3/Lambda …）。"""
    try:
        from kb.query import build_query

        return build_query(text, fingerprint)
    except Exception:  # noqa: BLE001 - 扩展失败退回原始拼接
        return f"{text} {fingerprint}".strip()


def _with_kb(result: dict) -> dict:
    """recon 结果附带离线 KB 命中与 FAST PATH：把「发现指纹 → 第一批动作」压到同一回合。"""
    fingerprint = json.dumps(result, ensure_ascii=False)[:1500]
    query = _kb_query(str(TASK.get("description") or ""), fingerprint)
    hits = kb_snippets(query, top_k=2)
    if hits:
        result["kb"] = hits[:4000]
    try:
        import playbooks

        target = TARGET_INFO.split(",")[0].strip()
        hint = playbooks.match_and_render(
            " ".join(
                [str(TASK.get("description") or ""), json.dumps(result, ensure_ascii=False)[:2000]]
            ),
            target,
        )
        if hint:
            result["fast_path"] = hint
    except Exception:  # noqa: BLE001 - 快速路径是增强，不是必需
        pass
    return result


def run_recon(args: dict) -> dict:
    """recon 工具实现；任何失败都以 {"error": ...} 返回，不打断主循环。"""
    try:
        import recon_recipe
    except Exception as exc:  # noqa: BLE001 - 工具缺失降级为错误结果
        return {"error": f"recon unavailable: {exc}"}
    mode = str(args.get("mode") or "").lower()
    target = str(args.get("target") or "").strip()
    url = str(args.get("url") or "").strip()
    try:
        if mode == "host":
            if not target:
                return {"error": "target required for mode=host"}
            return _with_kb({"host": recon_recipe.recon_host(target)})
        if mode == "web":
            if not url and target.startswith(("http://", "https://")):
                url = target
            if not url:
                return {"error": "url required for mode=web"}
            return _with_kb({"web": recon_recipe.recon_web(url)})
        if mode == "all":
            if not target:
                return {"error": "target required for mode=all"}
            host = recon_recipe.recon_host(target)
            web_url = url or recon_recipe.derive_web_url(target, host.get("open_ports") or [])
            return _with_kb({"host": host, "web": recon_recipe.recon_web(web_url)})
        return {"error": f"unknown mode: {mode!r}"}
    except Exception as exc:  # noqa: BLE001 - 网络/工具异常都降级
        return {"error": f"recon failed: {exc}"}


def run_rea(args: dict) -> dict:
    """REA（Ghidra）工具：把本地二进制的伪代码/概览变成模型可读的证据结果。"""
    path = str(args.get("path") or "").strip()
    mode = str(args.get("mode") or "inspect").lower()
    procedure = str(args.get("procedure") or "").strip()
    if not path:
        return {"error": "path required"}
    if not Path(path).is_file():
        return {"error": f"file not found: {path}"}
    if mode == "decompile" and not procedure:
        return {"error": "procedure required for mode=decompile"}
    if mode == "decompile":
        cmd = ["rea", "decompile", path, procedure, "--format", "json", "--token-limit", "12000"]
    elif mode == "inspect":
        cmd = ["rea", "inspect", path, "--format", "json"]
    else:
        return {"error": f"unknown mode: {mode!r}"}
    env = dict(os.environ)
    env.setdefault("GHIDRA_INSTALL_DIR", "/opt/tsecbench/ghidra")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=180, env=env
        )
    except FileNotFoundError:
        return {"error": "rea not installed"}
    except subprocess.TimeoutExpired:
        return {"error": "rea timed out (180s)"}
    if not proc.stdout.strip():
        return {"error": f"rea failed: {proc.stderr[-500:]}"}
    if mode == "inspect":
        return {"inspect": proc.stdout[:12000]}
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {"error": "rea returned non-JSON", "stderr": proc.stderr[-500:]}
    code = data.get("normalized_result") or data.get("raw_result") or ""
    return {
        "provider": (data.get("provider") or {}).get("name"),
        "procedure": (data.get("parameters") or {}).get("procedure"),
        "code": code[:12000],
        "confidence": data.get("confidence"),
        "limitations": (data.get("limitations") or [])[:5],
    }


def run_kb(args: dict) -> dict:
    """按需检索离线知识库（不把整段 KB 塞进系统提示词）。"""
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query required"}
    try:
        text = kb_snippets(query, top_k=int(args.get("top_k") or 3))
    except Exception as exc:  # noqa: BLE001 - 工具失败降级
        return {"error": f"kb failed: {exc}"}
    return {"query": query, "snippets": text[:8000]}


# ------------------------------------------------------- tool message hygiene ---
def summarize_history(messages: list[dict], limit: int = 12) -> str:
    """压缩前保留高密度执行状态：最近命令 → 退出码/耗时 → 首行输出。

    harness 生成，不依赖模型记忆：上下文裁剪后模型仍知道做过什么、结果如何，
    避免压缩后重复探测（实测大量会话 closed_reason=compacted 后仍在重跑）。
    """
    pending: dict[str, str] = {}
    entries: list[str] = []
    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                name = fn.get("name") or "tool"
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
                if name == "run_command":
                    cmd = str(args.get("command") or "").strip().replace("\n", " ")[:180]
                    pending[str(call.get("id"))] = f"$ {cmd}"
                else:
                    pending[str(call.get("id"))] = (
                        f"{name} {json.dumps(args, ensure_ascii=False)[:120]}"
                    )
        elif role == "tool":
            head = pending.pop(str(msg.get("tool_call_id")), None)
            if head is None:
                continue
            content = (msg.get("content") or "").replace("\n", " ")
            meta, sep, body = content.partition("]")
            if sep and meta.startswith("["):
                meta = meta[1:][:80]
            else:
                meta, body = "", content
            entries.append(f"{head} => {meta} | {body[:140]}")
    if not entries:
        return ""
    lines = ["[HARNESS STATE — preserved from trimmed history]"]
    lines.extend(entries[-limit:])
    reported = globals().get("_reported") or set()
    if reported:
        lines.append("flags reported: " + ", ".join(sorted(reported)))
    return "\n".join(lines)


def sanitize_tool_messages(messages: list[dict]) -> list[dict]:
    """修复 tool_calls 组的消息形状（DeepSeek 400 防线）。

    规则：每条带 tool_calls 的 assistant 之后必须紧跟全部对应 tool_call_id
    的 tool 消息；缺失的补占位，孤立的 tool 消息丢弃。用户提示插话、上下文
    裁剪都可能破坏该形状 —— 一旦破坏，后续每次请求都 400，历史永久中毒。
    """
    out: list[dict] = []
    i, n = 0, len(messages)
    while i < n:
        msg = messages[i]
        if msg.get("role") == "tool":
            i += 1  # 孤立 tool 消息：丢弃
            continue
        out.append(msg)
        i += 1
        calls = msg.get("tool_calls") or []
        if msg.get("role") != "assistant" or not calls:
            continue
        ids = [c.get("id") for c in calls if c.get("id")]
        seen: set[str] = set()
        while i < n and messages[i].get("role") == "tool":
            tid = messages[i].get("tool_call_id")
            if tid in ids and tid not in seen:
                out.append(messages[i])
                seen.add(tid)
            i += 1
        for cid in ids:
            if cid not in seen:
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": cid,
                        "content": "[tool result unavailable: call was interrupted]",
                    }
                )
    return out


# ----------------------------------------------------------- flag detection ---
NAMED_FLAG_PATTERNS = [
    re.compile(r"(?<![a-zA-Z])FLAG\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])flag\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])HTB\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])picoCTF\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])THM\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])CTF\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])XBEN\{[^}\n]{1,200}\}"),
]
# 通用兜底：CSS/JS 规则（body{color:#fff}、h1{border:...}、let{children:t}）
# 与 flag 同形，线上实测被误提交。正文限定为 flag 常见字符集，排除
# `: ; ( ) # , 空白` 等代码/CSS 语法。
GENERIC_FLAG_PATTERN = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9_]{2,24})\{([^}\n]{4,120})\}")
GENERIC_BODY_RE = re.compile(r"^[A-Za-z0-9_\-+=/.]{4,120}$")
# 通用兜底只认真实 flag 前缀；代码标识符（collect/it.name、
# JSAPI_Event_*_{uuid}）一律拒绝。
GENERIC_PREFIXES = frozenset({"flag", "ctf", "htb", "thm", "picoctf", "xben"})

_reported: set[str] = set()


def find_flags(text: str, include_generic: bool = False) -> list[str]:
    found: list[str] = []
    for pattern in NAMED_FLAG_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            if value not in found:
                found.append(value)
    if include_generic:
        for match in GENERIC_FLAG_PATTERN.finditer(text):
            if match.group(1).lower() not in GENERIC_PREFIXES:
                continue
            if not GENERIC_BODY_RE.match(match.group(2)):
                continue
            value = match.group(0)
            if value not in found:
                found.append(value)
    return found


def report_candidate(flag: str) -> None:
    """Print a candidate so BoxPwnr's external-solver scanner picks it up.

    编排器模式下同时落盘 out/flags/NNN.txt（§2.2：一个文件一个候选）。
    """
    if flag in _reported:
        return
    _reported.add(flag)
    print(flag, flush=True)
    print(f"<FLAG>{flag}</FLAG>", flush=True)
    if ORCH_MODE:
        global _flag_seq
        out_flags = Path(SOLVER_TASK_PATH).parent / "out" / "flags"
        out_flags.mkdir(parents=True, exist_ok=True)
        (out_flags / f"{_flag_seq:03d}.txt").write_text(flag + "\n", encoding="utf-8")
        _flag_seq += 1


def write_result(status: str, evidence: str = "") -> None:
    """编排器模式收尾：写 out/result.json（编排器不信任它，仅用于日志/回灌）。"""
    if not ORCH_MODE:
        return
    result = {
        "status": status,
        "candidates": sorted(_reported),
        "evidence": evidence,
        "tokens_used": TOTALS.get("prompt", 0) + TOTALS.get("completion", 0),
        "tools_used": [],
        "next_idea": "",
    }
    out = Path(SOLVER_TASK_PATH).parent / "out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_task() -> dict:
    """读 task.json（编排器模式）。损坏时降级为空任务，不崩。"""
    try:
        return json.loads(Path(SOLVER_TASK_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# -------------------------------------------------------------- knowledge ---
def kb_snippets(text: str, top_k: int = 3) -> str:
    """离线知识库检索（kb/*.md 倒排索引，毫秒级、零 LLM、零网络）。

    这是「简单题速通」路径：题目关键词 → 命中 cheatsheet / payload 库片段 →
    直接注入 prompt，模型不必自己摸索已知打法。任何失败（缺文件 / 导入错）
    静默降级为空串 —— 知识注入是增强，不是必需路径。
    """
    try:
        import sys

        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        from kb.index import Index

        hits = Index().query(text, top_k=top_k)
    except Exception:
        return ""
    if not hits:
        return ""
    blocks = [f"## KB: {h['doc']}/{h['section']}\n{h['body']}" for h in hits]
    return (
        "# KNOWLEDGE — APPLY FIRST (offline cheatsheet hits for THIS task)\n"
        "If a section below matches this challenge, your FIRST command MUST apply its payload "
        "directly (substitute the target host/port). Do NOT re-derive or spend turns exploring "
        "what the cheatsheet already states — running the payload IS the verification. Only "
        "improvise after the known technique has been tried and observed to fail.\n\n"
        + "\n\n".join(blocks)
    )


# ------------------------------------------------------------------- prompt ---
def build_system_prompt() -> str:
    base = TASK_PROMPT.strip()
    contract = """
# OPERATING CONTRACT
You drive a lean Linux box through four tools:
- run_command: shell (bash/python3), non-interactive, bounded.
- recon: nmap host scan / web headers+tech / capped 404-filtered dir-bust (JSON).
- rea: Ghidra evidence — `inspect` a local binary or `decompile` one procedure.
- kb: offline security knowledge base (payloads, CVE notes, SOP checklists). Query it before guessing.

Hard rules:
- No internet, no installs, no OOB callbacks; the only reachable endpoints are the target(s) and the LLM API.
- Target connectivity is preconfigured. A first timeout usually means cold start: retry once after ~20s, then change vector. Do not debug VPN/DNS/routing.
- Bound every network command (`curl -m 5`, `timeout 30`). One high-information command beats many small ones.
- Flags: report exactly as found; multi-flag challenges continue until all flags are submitted.
- No hints exist. Solve from local analysis; never probe the platform or LLM side.
- The harness enforces a per-command wall cap and reports remaining slice time; pace yourself.

# ATTACK LOOP
- New fingerprint or credential: try the fastest viable exploit first, then widen recon.
- Any credential/token in output: immediately reuse it on other discovered services and endpoints.
- Same vector fails twice: switch to the next concrete candidate; never repeat the same request shape.
- Multi-flag: every flag is a new starting point — re-enumerate what the current assets can reach.
"""
    return (base + "\n\n" + contract.strip()) if base else contract.strip()


# ------------------------------------------------------- orchestrator mode ---
def setup_orch_mode() -> None:
    """编排器模式：task.json 注入目标与上下文；时间片截断自 SOLVER_DEADLINE_EPOCH。"""
    global TARGET_INFO, TASK_PROMPT, MAX_MINUTES, DEADLINE_EPOCH, TASK
    task = load_task()
    TASK = task
    targets = task.get("targets") or []
    if targets:
        TARGET_INFO = ", ".join(str(t) for t in targets)
    parts = []
    if task.get("description"):
        parts.append(f"# Challenge description\n{task['description']}")
    if task.get("flag_count", 1) > 1:
        correct = task.get("correct_flag_count", 0)
        parts.append(
            f"# Multi-flag challenge: {task['flag_count']} flags total, "
            f"{correct} already submitted. Do NOT stop after the first flag."
        )
    if task.get("attempt", 1) > 1:
        parts.append(f"# Attempt #{task['attempt']}: try a DIFFERENT strategy than before.")
    prior = task.get("prior") or {}
    for key, title in (
        ("assets", "Confirmed assets"),
        ("explored", "Already explored (do NOT repeat)"),
        ("open_leads", "Open leads"),
        ("credentials", "Confirmed credentials"),
        ("findings", "Findings (versions/endpoints)"),
    ):
        items = prior.get(key) or []
        if items:
            parts.append(f"# {title}\n" + json.dumps(items, ensure_ascii=False)[:4000])
    history = str(prior.get("history") or "").strip()
    if history:
        parts.append("# Previous attempt (harness state)\n" + history[:4000])
    if parts:
        TASK_PROMPT = (TASK_PROMPT + "\n\n" if TASK_PROMPT else "") + "\n\n".join(parts)
    # 启动期知识注入：main.py 是线上入口，不像 dual_main 有 profile/skills 层；
    # 先把与题面最相关的离线 KB 片段直接放进 prompt，减少「盲目 curl」。
    knowledge = kb_snippets(
        _kb_query(
            str(task.get("description") or ""),
            " ".join(str(target) for target in targets),
        ),
        top_k=2,
    )
    if knowledge:
        TASK_PROMPT = (TASK_PROMPT + "\n\n" + knowledge).strip()
    try:
        import playbooks

        target = str(targets[0]) if targets else ""
        hint = playbooks.match_and_render(
            " ".join([task.get("description") or "", *targets]), target
        )
        if hint:
            TASK_PROMPT = (TASK_PROMPT + "\n\n" + hint).strip()
    except Exception:  # noqa: BLE001 - 快速路径是增强，不是必需
        pass
    deadline_epoch = os.environ.get("SOLVER_DEADLINE_EPOCH")
    if deadline_epoch:
        try:
            DEADLINE_EPOCH = float(deadline_epoch)
        except (TypeError, ValueError):
            DEADLINE_EPOCH = None
        if DEADLINE_EPOCH is not None:
            MAX_MINUTES = max(0.5, min(MAX_MINUTES, (DEADLINE_EPOCH - time.time()) / 60))
