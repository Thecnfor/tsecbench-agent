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
CMD_TIMEOUT = int(cfg("SOLVER_CMD_TIMEOUT", "120"))
OUTPUT_LIMIT = int(cfg("SOLVER_OUTPUT_LIMIT", "12000"))

TASK_PROMPT = cfg("BOXPWNR_PROMPT", "") or ""
TARGET_INFO = cfg("BOXPWNR_TARGET_IP", "") or ""
CONTAINER = cfg("BOXPWNR_CONTAINER", "") or ""

# ---- 编排器模式（architecture.md §2.1/§2.2）----
# SOLVER_TASK 指向 task.json 且没有 BOXPWNR_* 时，走编排器契约：
# 读 task.json → 解题 → 候选写 out/flags/*.txt + out/result.json。
SOLVER_TASK_PATH = cfg("SOLVER_TASK", "") or ""
ORCH_MODE = bool(SOLVER_TASK_PATH) and not TASK_PROMPT
_flag_seq = 0
TOTALS: dict = {}  # LLM 用量统计（write_result 收尾用）


# ---------------------------------------------------------------- executor ---
def run_command(command: str, timeout: int) -> dict:
    """Run a shell command inside the challenge container (or locally).

    Uses `timeout` inside the container as first line of defense, and kills the
    whole process group host-side as the backstop (same discipline as the final
    architecture: kill the process group, never leave stragglers).
    """
    if CONTAINER:
        argv = [
            "docker",
            "exec",
            "-i",
            CONTAINER,
            "timeout",
            "-k",
            "5",
            str(timeout),
            "bash",
            "-lc",
            command,
        ]
    else:
        argv = ["bash", "-lc", command]

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

    try:
        out, _ = proc.communicate(timeout=timeout + 20)
        timed_out = False
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        timed_out = True

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


# ----------------------------------------------------------- flag detection ---
NAMED_FLAG_PATTERNS = [
    re.compile(r"(?<![a-zA-Z])FLAG\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])HTB\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])picoCTF\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])THM\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])CTF\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])XBEN\{[^}\n]{1,200}\}"),
]
GENERIC_FLAG_PATTERN = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_]{2,24}\{[^}\n]{4,120}\}")

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
    except OSError, json.JSONDecodeError:
        return {}


# ------------------------------------------------------------------- prompt ---
def build_system_prompt() -> str:
    base = TASK_PROMPT.strip()
    if CONTAINER:
        execution = (
            "- You act exclusively through the `run_command` tool. "
            "Every command runs inside your Kali Linux container."
        )
    else:
        execution = (
            "- You act exclusively through the `run_command` tool. Commands run on a LEAN host "
            "(not a Kali container). Do NOT assume any specific tool exists — inventory first "
            "(`command -v nmap gobuster ffuf curl python3`), then pick your approach; "
            "no tool? hand-roll it in bash/python3 (all stdlib, no installs). Dirb wordlists "
            "may be at /usr/share/dirb/wordlists/common.txt; otherwise build a focused one "
            "from the site's own links in one bash loop. "
            "NEVER run broad filesystem searches (`find /`, `locate`) — they only burn your budget."
        )
    contract = f"""

# RUNTIME CONSTRAINTS (hard limits of the evaluation sandbox)
- Outbound internet is UNAVAILABLE in the runtime container. NEVER try to download or update
  anything: no `apt`, `pip`, `npm`, `gem`, `go install`, no `git clone` from remote hosts,
  no fetching tools/wordlists/archives from the internet.
- Online services are out of reach: no OOB callbacks (interact.sh, dnslog, request bins,
  pastebin, webhook.site), no passive-recon APIs (shodan, censys, virustotal). Blind-SSRF /
  exfil tactics that assume an internet callback cannot work here — prove things locally
  instead (timing, response lengths, direct output).
- Only use: your own bash/python3 scripts + tools already baked into this image.
  If a capability is missing, synthesize it with python3 stdlib or plain bash.
- The LLM API endpoint is your one sanctioned non-target endpoint. Never probe, scan or
  payload the platform/LLM side; all attack energy goes to the challenge target only.
# EXECUTION CONTRACT (added by the solver shell)
{execution}
- Commands must be non-interactive and automated. Use explicit timeouts for long tasks, e.g. `timeout 90 nmap -sV -p- <target>`.
- You may create files (e.g. `cat > /root/exploit.py <<'EOF' ... EOF`) and run them.
- If a command runs too long the shell kills it; adjust and retry with a narrower scope.
- Do NOT search the internet for writeups, solutions or flags. Solve with local analysis only.
- When you have found the flag and verified it, produce a final answer containing the flag exactly
  as discovered (e.g. FLAG{...}) and stop calling tools.
"""
    return (base + contract) if base else contract.strip()


# ------------------------------------------------------- orchestrator mode ---
def setup_orch_mode() -> None:
    """编排器模式：task.json 注入目标与上下文；时间片截断自 SOLVER_DEADLINE_EPOCH。"""
    global TARGET_INFO, TASK_PROMPT, MAX_MINUTES
    task = load_task()
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
    if task.get("hint"):
        parts.append(f"# Hint (score already penalized, converge fast)\n{task['hint']}")
    prior = task.get("prior") or {}
    for key, title in (
        ("assets", "Confirmed assets"),
        ("explored", "Already explored (do NOT repeat)"),
        ("open_leads", "Open leads"),
    ):
        items = prior.get(key) or []
        if items:
            parts.append(f"# {title}\n" + json.dumps(items, ensure_ascii=False)[:4000])
    if parts:
        TASK_PROMPT = (TASK_PROMPT + "\n\n" if TASK_PROMPT else "") + "\n\n".join(parts)
    deadline_epoch = os.environ.get("SOLVER_DEADLINE_EPOCH")
    if deadline_epoch:
        remaining = float(deadline_epoch) - time.time()
        MAX_MINUTES = max(0.5, min(MAX_MINUTES, remaining / 60))
