"""Solver 契约原语共享层（从 main.py 抽取，dual_main.py / main.py 共用）。

单一真值源：所有契约状态（ORCH_MODE / TASK_PROMPT / _reported / TOTALS …）
都住在本模块；main.py 通过模块级 __getattr__ 转发访问（test_solver_contract.py
与 pi_main.py 的 `mod.XXX` 访问形态保持不变，且 setup_orch_mode 的变更对
两个消费方立即可见）。

契约本体见 architecture.md §2.1-2.4：
  读 SOLVER_TASK(task.json) → 干活 → out/flags/NNN.txt + out/result.json → exit 0。
"""

from __future__ import annotations

import atexit
import contextlib
import html as html_lib
import itertools
import json
import os
import re
import select
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
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
# 上下文预算（三层）：软阈值降级旧轮次、硬阈值整段压缩、条数是最后保险丝。
# DeepSeek 128k 上下文下 90k 硬顶留 38k 余量；压缩目标 40k 形成迟滞，
# 避免「压完仍超阈值 → 每轮重压」的 thrash。
TRIM_TOKENS = int(cfg("SOLVER_TRIM_TOKENS", "90000"))
TRIM_SOFT_TOKENS = int(cfg("SOLVER_TRIM_SOFT_TOKENS", "60000"))
TRIM_TARGET_TOKENS = int(cfg("SOLVER_TRIM_TARGET_TOKENS", "40000"))
TRIM_MAX_MSGS = int(cfg("SOLVER_TRIM_MAX_MSGS", "400"))
KEEP_RECENT_TURNS = int(cfg("SOLVER_KEEP_RECENT_TURNS", "12"))
EMERGENCY_TOKENS = int(cfg("SOLVER_EMERGENCY_TOKENS", "20000"))
EMERGENCY_MSG_TOKENS = int(cfg("SOLVER_EMERGENCY_MSG_TOKENS", "2000"))
MAX_CONTEXT_RECOVERIES = int(cfg("SOLVER_MAX_CONTEXT_RECOVERIES", "2"))
MAX_CONSECUTIVE_LLM_ERRORS = int(cfg("SOLVER_MAX_CONSECUTIVE_LLM_ERRORS", "8"))
# 单条工具输出超过阈值即落盘，上下文只回预览 + 文件路径；
# 单轮所有工具输出共享一个字符预算，防止一轮多个调用把上下文打爆。
SPILL_CHARS = int(cfg("SOLVER_SPILL_CHARS", "6000"))
SPILL_PREVIEW_CHARS = int(cfg("SOLVER_SPILL_PREVIEW_CHARS", "2000"))
TURN_OUTPUT_BUDGET = int(cfg("SOLVER_TURN_OUTPUT_CHARS", "20000"))
# 连续「只说不做」的停机阈值：3 太早，实测会把已拿到 LFI/凭据的链子掐断。
MAX_PLAIN_NUDGES = int(cfg("SOLVER_MAX_PLAIN_NUDGES", "6"))
# 收敛守卫：连续多少轮没有新资产/凭据/端点就提示换面；多少轮无新事实直接收尾。
NO_FACT_NUDGE_TURNS = int(cfg("SOLVER_NO_FACT_NUDGE_TURNS", "8"))
STALE_HARD_TURNS = int(cfg("SOLVER_STALE_HARD_TURNS", "30"))
MAX_NO_FACT_NUDGES = int(cfg("SOLVER_MAX_NO_FACT_NUDGES", "2"))
# c-09 实测 82 轮才解出：绝对轮数上限不能太低，否则误杀可解题。
MAX_TURNS_NO_FLAG = int(cfg("SOLVER_MAX_TURNS_NO_FLAG", "100"))


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
TOOL_USAGE: dict[str, int] = {}  # 各工具调用次数（write_result 收尾用）
TASK: dict = {}  # 当前 task.json（setup_orch_mode 写入）


def note_tool_use(name: str) -> None:
    TOOL_USAGE[name] = TOOL_USAGE.get(name, 0) + 1


def turn_limit() -> int:
    """轮数上限。编排器模式以时间片为唯一预算（deadline 由编排器强杀兜底），
    60 轮只是 BoxPwnr 直跑模式的保险丝 —— 难题常需要上百轮工具交互。"""
    return 10**9 if ORCH_MODE else MAX_TURNS


# ---------------------------------------------------------------- executor ---
PERSISTENT_SHELL = (cfg("SOLVER_PERSISTENT_SHELL", "on") or "").strip().lower() in (
    "1",
    "on",
    "true",
    "yes",
) and os.name == "posix"
_SHELL_LOCK = threading.Lock()
_SHELL: _PersistentShell | None = None


class _PersistentShell:
    """长驻 bash：变量/cwd/导出跨 run_command 保留（SWE-agent ACI 思路）。

    每条命令后打印唯一 sentinel + `$?`；超时/异常时杀整个进程组并重启。
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.start()

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["bash", "--noprofile", "--norc"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=os.getcwd(),
            env=os.environ.copy(),
            bufsize=0,
            start_new_session=True,
        )
        with contextlib.suppress(OSError):
            if SOLVER_TASK_PATH:
                pid_path = Path(SOLVER_TASK_PATH).parent / "out" / "shell.pid"
                pid_path.parent.mkdir(parents=True, exist_ok=True)
                pid_path.write_text(str(self.proc.pid), encoding="utf-8")

    def stop(self) -> None:
        if self.proc is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            self.proc.wait(timeout=2)
        with contextlib.suppress(OSError):
            if SOLVER_TASK_PATH:
                (Path(SOLVER_TASK_PATH).parent / "out" / "shell.pid").unlink()
        self.proc = None

    def run(self, command: str, timeout: int) -> dict:
        with _SHELL_LOCK:
            if self.proc is None or self.proc.poll() is not None:
                self.stop()
                self.start()
            sentinel = f"__TSEC_DONE_{os.getpid()}_{time.time_ns()}__"
            # 命令组 + stdin 重定向：交互式命令（cat/nc/mysql）若直接继承 shell 的
            # stdin，会吞掉下一条命令与哨兵，导致整条命令超时或伪造 exit 0。
            # 组内变量仍然保留（不破持久 shell 语义），heredoc 也不受影响。
            payload = f"{{ {command}\n}} </dev/null\nprintf '\\n%s%s\\n' '{sentinel}' \"$?\"\n"
            started = time.time()
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(payload.encode("utf-8"))
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self.stop()
                self.start()
                return {
                    "exit_code": 127,
                    "output": "shell restarted",
                    "timed_out": False,
                    "seconds": 0.0,
                }
            buffer = b""
            marker = sentinel.encode()
            deadline = time.time() + timeout
            assert self.proc.stdout is not None
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    self.stop()
                    self.start()
                    return {
                        "exit_code": 124,
                        "output": buffer.decode("utf-8", errors="replace"),
                        "timed_out": True,
                        "seconds": round(time.time() - started, 1),
                    }
                ready, _, _ = select.select([self.proc.stdout], [], [], min(0.2, remaining))
                if not ready:
                    continue
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    exit_code = self.proc.returncode or 1
                    self.stop()
                    self.start()
                    return {
                        "exit_code": exit_code,
                        "output": buffer.decode("utf-8", errors="replace"),
                        "timed_out": False,
                        "seconds": round(time.time() - started, 2),
                    }
                buffer += chunk
                index = buffer.find(marker)
                if index < 0:
                    continue
                after = buffer[index + len(marker) :]
                code_line, _, _rest = after.partition(b"\n")
                try:
                    exit_code = int(code_line.strip() or b"0")
                except ValueError:
                    exit_code = 0
                output = buffer[:index]
                if output.startswith(b"\n"):
                    output = output[1:]
                return {
                    "exit_code": exit_code,
                    "output": output.decode("utf-8", errors="replace"),
                    "timed_out": False,
                    "seconds": round(time.time() - started, 2),
                }


def reset_shell_session() -> None:
    """测试/收尾用：杀掉长驻 shell。"""
    global _SHELL
    if _SHELL is not None:
        _SHELL.stop()
        _SHELL = None


atexit.register(reset_shell_session)


_FOREIGN_PATH_RE = re.compile(r"(?:/mnt/|/opt/tsecbench/workspaces|/workspaces/)")
_LOCAL_ESCAPE_RE = re.compile(
    r"(?i)\b(?:grep|rg|find|ls|cat|head|tail|wc|sort|awk|sed)\b[^\n]*"
    r"(?:^|\s)\.\.(?:/|\s|$)"
)
_ROOT_SCAN_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:find|grep|rg|du|tar)\s+(?:-[^\s]+\s+)*/(?:\s|$|mnt\b)"
)
_TMP_REF_RE = re.compile(r"(?<![\w$])/tmp(?:/|\b)")


def _local_tmp_reference(command: str) -> bool:
    """引号外引用共享 /tmp 视为违规；靶机 payload 里的 /tmp 不算。"""
    stripped = re.sub(r"'[^']*'|\"[^\"]*\"", " ", command)
    return bool(_TMP_REF_RE.search(stripped))


def run_command(command: str, timeout: int, *, isolated: bool = False) -> dict:
    """Run a shell command inside the challenge container (or locally).

    **默认使用长驻 bash**（变量/cwd 跨命令保留）；每条命令带唯一 sentinel
    与 `$?`，超时杀整个进程组并重启。容器模式/关闭开关/`isolated=True`
    时退回逐条 `timeout -k` 执行（harness 内部探针用 isolated，避免探针
    超时重启用户会话、丢掉 cwd/变量）。

    逐条路径的说明：实测 solver 常写无超时的命令（如 `curl -s -i http://T/`），
    目标挂死时只有宿主侧 `t+20` 的粗杀兜底 —— 一条命令白烧 120s+ 时间片。
    内层 timeout 让超时精确到 t；外层仍 kill 整个进程组防孤儿。
    """
    own_ws = str(Path(SOLVER_TASK_PATH).parent) if SOLVER_TASK_PATH else ""
    if _FOREIGN_PATH_RE.search(command) and own_ws and own_ws not in command:
        return {
            "exit_code": 126,
            "output": "blocked: other task workspace; only your own workspace is readable",
            "timed_out": False,
            "seconds": 0.0,
        }
    if _LOCAL_ESCAPE_RE.search(command):
        return {
            "exit_code": 126,
            "output": "blocked: parent-directory scan; stay inside your workspace",
            "timed_out": False,
            "seconds": 0.0,
        }
    if _local_tmp_reference(command):
        return {
            "exit_code": 126,
            "output": "blocked: shared /tmp is not per-task; use $TMPDIR for scratch files",
            "timed_out": False,
            "seconds": 0.0,
        }
    if _ROOT_SCAN_RE.search(command):
        return {
            "exit_code": 126,
            "output": (
                "root-wide scan blocked: search /app, /root, or your workspace only (never /)"
            ),
            "timed_out": False,
            "seconds": 0.0,
        }
    if PERSISTENT_SHELL and not CONTAINER and not isolated:
        global _SHELL
        if _SHELL is None:
            _SHELL = _PersistentShell()
        return _SHELL.run(command, timeout)

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

    out = out or ""
    return {
        "exit_code": proc.returncode,
        "output": out,
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
def parse_tool_arguments(raw: str) -> tuple[dict | None, str | None]:
    """解析模型给的工具参数；非对象/坏 JSON 一律返回可回填的 tool error。"""
    try:
        args = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        return None, f"invalid tool arguments: {exc}"
    if not isinstance(args, dict):
        return None, "tool arguments must be a JSON object"
    return args, None


def _http_json(url: str, *, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            return {
                "status": response.status,
                "body": response.read().decode("utf-8", "replace")[:8000],
            }
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": exc.read().decode("utf-8", "replace")[:8000]}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def run_gremlin(args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    query = str(args.get("query") or "").strip()
    if not url or not query:
        return {"error": "url and query required"}
    return _http_json(url, method="POST", body={"gremlin": query})


def run_gradio(args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    path = str(args.get("path") or "/config").strip()
    if not url:
        return {"error": "url required"}
    endpoint = url.rstrip("/") + "/" + path.lstrip("/")
    data = args.get("data")
    return _http_json(endpoint, method="POST" if data is not None else "GET", body=data)


def run_stage(args: dict) -> dict:
    """多阶段状态：读写 out/stage_state.json，作为跨 attempt 的 relay 协议。"""
    if not SOLVER_TASK_PATH:
        return {"error": "stage state requires orchestrator mode"}
    path = Path(SOLVER_TASK_PATH).parent / "out" / "stage_state.json"
    state = {
        "stage": "recon",
        "goal": "",
        "assets": [],
        "creds": [],
        "flags": [],
        "next": "",
        "failed": [],
    }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            state.update(data)
    except (OSError, ValueError):
        pass
    if str(args.get("action") or "get").lower() == "update":
        for key in ("stage", "goal", "next"):
            if args.get(key) is not None:
                state[key] = str(args[key])[:300]
        for key in ("assets", "creds", "flags", "failed"):
            if isinstance(args.get(key), list):
                state[key] = list(dict.fromkeys([*(state.get(key) or []), *args[key]]))[-50:]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def build_tool_specs() -> list[dict]:
    """模型可见的工具面（能力在 harness 里，不靠提示词叮嘱）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "run_command",
                "description": (
                    "Run a shell command and return combined stdout/stderr. stdin is closed "
                    "(non-interactive only; pass input via arguments, heredocs, or files). "
                    + (
                        "Commands share one persistent bash session (cwd/vars persist until a "
                        "timeout restarts it)."
                        if PERSISTENT_SHELL and not CONTAINER
                        else "Each command runs in a fresh shell."
                    )
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
                    "Start here on any new target: deterministic recon returning compact JSON. "
                    "mode=host runs a capped nmap service scan; mode=web fetches headers/title/tech "
                    "and runs a time-capped, 404-filtered directory sweep; mode=all does both and "
                    "derives the web URL from open ports. Prefer this FIRST over hand-writing long "
                    "nmap/gobuster pipelines; use run_command for targeted follow-up requests."
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
                    "Search the offline security knowledge base (payloads, CVE notes, SOP "
                    "checklists) and return the best matching snippets. Call it when you are "
                    "stuck, or before crafting a payload for a known product/CVE, to get exact "
                    "syntax instead of guessing."
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
        {
            "type": "function",
            "function": {
                "name": "stage",
                "description": "Get/update multi-stage relay state (stage, goal, assets, creds, flags, next, failed).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["get", "update"]},
                        "stage": {"type": "string"},
                        "goal": {"type": "string"},
                        "assets": {"type": "array", "items": {"type": "string"}},
                        "creds": {"type": "array", "items": {"type": "string"}},
                        "flags": {"type": "array", "items": {"type": "string"}},
                        "next": {"type": "string"},
                        "failed": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["action"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "gremlin",
                "description": "POST a Gremlin/Groovy query to a Gremlin endpoint and return status/body.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Full /gremlin URL"},
                        "query": {"type": "string", "description": "Gremlin/Groovy query"},
                    },
                    "required": ["url", "query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "gradio",
                "description": "Call a Gradio/Langflow endpoint (config, version, validate/code, predict).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Base URL"},
                        "path": {
                            "type": "string",
                            "description": "e.g. /config or /api/v1/version",
                        },
                        "data": {"type": "object", "description": "Optional JSON body (POST)"},
                    },
                    "required": ["url"],
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


_AUTO_PROBED: set[str] = set()
_AUTO_PROBED_LOCK = threading.Lock()


_KNOWN_TERM_RE = re.compile(
    r"\b(dify|langflow|gitea|forgejo|hugegraph|gradio|jenkins|minio|azurite|next\.js|"
    r"flask|werkzeug|1panel|weaver|geoserver|ollama|comfyui|pydash|serverless)\b|"
    r"\bCVE-\d{4}-\d{4,7}\b",
    re.I,
)


def detect_known_terms(text: str) -> list[str]:
    """从模型自己的话里识别产品/CVE；用于强制检索离线 wiki 的精确 payload。"""
    terms = []
    for match in _KNOWN_TERM_RE.finditer(text):
        term = match.group(0)
        term = term.upper() if term.upper().startswith("CVE-") else term.lower()
        if term not in terms:
            terms.append(term)
    return terms


def known_issue_snippet(term: str) -> str | None:
    """返回包含该产品/CVE 的 KB 片段；没有命中就静默跳过。"""
    try:
        text = kb_snippets(f"{term} payload exploit CVE", top_k=3)
    except Exception:  # noqa: BLE001 - KB 缺失不影响主循环
        return None
    if not text or term.lower() not in text.lower():
        return None
    return text[:1500]


def _with_kb(result: dict) -> dict:
    """recon 结果附带离线 KB 命中与 FAST PATH：把「发现指纹 → 第一批动作」压到同一回合。"""
    fingerprint = json.dumps(result, ensure_ascii=False)[:1500]
    query = _kb_query(str(TASK.get("description") or ""), fingerprint)
    hits = kb_snippets(query, top_k=2)
    if hits:
        result["kb"] = hits[:4000]
        known = [line.strip() for line in hits.splitlines() if line.startswith("## KB:")][:5]
        if known:
            result["known_issues"] = known
    try:
        import playbooks

        target = TARGET_INFO.split(",")[0].strip()
        combined = " ".join([str(TASK.get("description") or ""), fingerprint[:2000]])
        playbook = playbooks.match(combined)
        if playbook:
            result["fast_path"] = playbooks.render(playbook, target)
            probes = playbooks.safe_probes(playbook, target)
            remaining = remaining_seconds()
            if remaining is not None and remaining < 5:
                probes = []
            if probes:
                with _AUTO_PROBED_LOCK:
                    should_probe = playbook["id"] not in _AUTO_PROBED
                    if should_probe:
                        _AUTO_PROBED.add(playbook["id"])
            if probes and should_probe:
                total_budget = 24 if remaining is None else max(2, min(24, int(remaining / 3)))
                per_probe = max(2, min(6, total_budget // max(1, len(probes))))
                deadline = time.time() + total_budget
                collected = []
                for probe_cmd in probes:
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    probe = run_command(probe_cmd, max(2, min(per_probe, int(left))), isolated=True)
                    output = str(probe.get("output") or "").strip()
                    if output:
                        collected.append({"cmd": probe_cmd, "output": truncate(output, 1200)})
                if collected:
                    result["fast_probe"] = collected
        import skills

        skill = skills.match(combined + " " + fingerprint[:2000]) or skills.generic_for(
            combined, target
        )
        if skill:
            result["skill"] = skills.render(skill, target)
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
            host = recon_recipe.recon_host(target)
            result: dict = {"host": host}
            # host-only 会漏掉 title/tech，KB 就只能按泛化描述检索。HTTP 端口
            # 开放时自动补一次 web 指纹，让 Dify/Gitea/Flask 等产品命中 wiki。
            web_url = url or recon_recipe.derive_web_url(target, host.get("open_ports") or [])
            if web_url:
                with contextlib.suppress(Exception):
                    result["web"] = recon_recipe.recon_web(web_url)
            return _with_kb(result)
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


def _rea_code(obj) -> str:
    """从 REA JSON 信封里递归找反编译文本（不同版本字段名会变）。"""
    if isinstance(obj, dict):
        for key in ("normalized_result", "raw_result", "code", "decompilation", "result", "text"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value
        for value in obj.values():
            found = _rea_code(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _rea_code(value)
            if found:
                return found
    return ""


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
        cmd = ["rea", "decompile", path, procedure, "--format", "json"]
    elif mode == "inspect":
        cmd = ["rea", "inspect", path, "--format", "json"]
    else:
        return {"error": f"unknown mode: {mode!r}"}
    env = dict(os.environ)
    env.setdefault("GHIDRA_INSTALL_DIR", "/opt/tsecbench/ghidra")
    remaining = remaining_seconds()
    if remaining is not None and remaining < 5:
        return {"error": "insufficient slice time for rea"}
    timeout = 180 if remaining is None else max(5, min(180, int(remaining * 0.5)))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout, env=env
        )
    except FileNotFoundError:
        return {"error": "rea not installed"}
    except subprocess.TimeoutExpired:
        return {"error": f"rea timed out ({timeout}s)"}
    if not proc.stdout.strip():
        return {"error": f"rea failed: {proc.stderr[-500:]}"}
    text = proc.stdout.strip()
    if mode == "inspect":
        return {"inspect": text[:12000]}
    try:
        data = json.loads(text)
    except ValueError:
        return {"output": text[:12000], "stderr": proc.stderr[-500:]}
    if isinstance(data, dict) and data.get("error"):
        return {"error": str(data.get("message") or data.get("error")), "code": data.get("code")}
    code = _rea_code(data)
    if not code:
        return {"output": text[:12000]}
    provider = data.get("provider") if isinstance(data.get("provider"), dict) else {}
    return {
        "provider": provider.get("name"),
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


STRUCTURED_TOOLS = ("recon", "rea", "kb", "gremlin", "gradio", "stage")


def run_structured_tool(name: str, args: dict) -> dict:
    if name == "recon":
        return run_recon(args)
    if name == "rea":
        return run_rea(args)
    if name == "kb":
        return run_kb(args)
    if name == "stage":
        return run_stage(args)
    if name == "gremlin":
        return run_gremlin(args)
    if name == "gradio":
        return run_gradio(args)
    return {"error": f"unknown structured tool: {name}"}


def run_structured_batch(calls: list[dict]) -> list[dict]:
    """一轮内的结构化工具调用并发执行，结果顺序与输入一致。

    仅限 recon/rea/kb（只读或本地、无共享 shell 状态）。run_command 有执行
    顺序语义，仍由主循环串行执行。
    """
    items = list(calls)
    if len(items) <= 1:
        return [run_structured_tool(item["name"], item["args"]) for item in items]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(4, len(items))) as pool:
        return list(pool.map(lambda item: run_structured_tool(item["name"], item["args"]), items))


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
                args, _error = parse_tool_arguments(fn.get("arguments") or "{}")
                if args is None:
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


# ------------------------------------------------------ context budgeting ---
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


def estimate_tokens(text: str) -> int:
    """无依赖 token 估算：CJK 按 1 字/token，其余按 ~4 字符/token。

    只用来自检上下文预算（真实计费仍以 API usage 为准）。对中英混排的
    工具输出比纯字符截断准确得多。
    """
    text = str(text or "")
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    return cjk + (len(text) - cjk + 3) // 4


def message_tokens(message: dict) -> int:
    total = estimate_tokens(str(message.get("content") or ""))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += estimate_tokens(str(function.get("name") or ""))
        total += estimate_tokens(str(function.get("arguments") or ""))
    return total + 4


def conversation_tokens(messages: list[dict]) -> int:
    return sum(message_tokens(message) for message in messages)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按 token 上限截断，保留头 2/3 + 尾 1/3（二分找字符边界）。"""
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    reserve = max(1, max_tokens - 8)
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= reserve:
            lo = mid
        else:
            hi = mid - 1
    head = lo * 2 // 3
    tail = lo - head
    marker = f"\n...[truncated to ~{max_tokens} tokens]...\n"
    return text[:head] + marker + (text[-tail:] if tail else "")


def _cap_arguments(raw: str, max_tokens: int) -> str:
    """参数超限时替换为合法 JSON 占位；截断原字符串会把历史 tool_call 变成坏 JSON。"""
    if estimate_tokens(raw) <= max_tokens:
        return raw
    return json.dumps({"_elided": True, "reason": "context budget"})


def _cap_message(message: dict, max_tokens: int) -> dict:
    out = dict(message)
    if out.get("content"):
        out["content"] = _truncate_to_tokens(str(out["content"]), max_tokens)
    calls = out.get("tool_calls") or []
    if calls:
        per_call = max(200, max_tokens // max(1, len(calls)))
        capped = []
        for call in calls:
            call = dict(call)
            function = dict(call.get("function") or {})
            if function.get("arguments"):
                function["arguments"] = _cap_arguments(str(function["arguments"]), per_call)
            call["function"] = function
            capped.append(call)
        out["tool_calls"] = capped
    return out


def _safe_cut(body: list[dict], cut: int) -> int:
    """把切点挪到合法消息边界：不能以 tool 结果开头（必须带它的 assistant）。"""
    if not body:
        return 0
    cut = max(0, min(cut, len(body) - 1))
    while cut < len(body) and body[cut].get("role") == "tool":
        cut += 1
    if cut >= len(body):
        for index in range(len(body) - 1, -1, -1):
            if body[index].get("role") == "assistant":
                return index
        return len(body) - 1
    return cut


def compact_history(
    messages: list[dict], *, target_tokens: int | None = None, summary_limit: int = 24
) -> tuple[list[dict], int, int, int]:
    """按 token 下水位压缩历史，返回 (新历史, 丢弃条数, 压缩前 tokens, 压缩后 tokens)。

    与旧实现的区别：摘要只覆盖**真正被丢弃的区间**（旧代码摘要窗口与保留尾部
    重叠，早期命令被无声丢掉）；尾部按 token 预算而不是固定条数保留；单条超大
    消息会被截断，保证压缩后一定落在目标附近。
    """
    target = int(TRIM_TARGET_TOKENS if target_tokens is None else target_tokens)
    before = conversation_tokens(messages)
    if len(messages) <= 2:
        return list(messages), 0, before, before
    head = list(messages[:2])
    body = list(messages[2:])
    budget = max(500, target - conversation_tokens(head) - 1500)
    kept_tokens = 0
    cut = len(body)
    for index in range(len(body) - 1, -1, -1):
        tokens = message_tokens(body[index])
        if cut < len(body) and kept_tokens + tokens > budget:
            break
        kept_tokens += tokens
        cut = index
    cut = _safe_cut(body, cut)
    if cut <= 0:
        return list(messages), 0, before, before
    dropped = body[:cut]
    max_message_tokens = max(EMERGENCY_MSG_TOKENS, target // 2)
    kept = [_cap_message(message, max_message_tokens) for message in body[cut:]]
    digest = summarize_history(dropped, limit=summary_limit)
    if digest:
        # 摘要本身也要受目标预算约束：小目标下不能出现"摘要比正文还大"。
        digest = _truncate_to_tokens(digest, max(200, target // 4))
    carried = [
        str(message.get("content"))
        for message in dropped
        if message.get("role") == "user"
        and str(message.get("content") or "").startswith("[context trimmed")
    ]
    parts = [
        "[context trimmed: older tool exchanges removed; full outputs remain in workspace files]"
    ]
    if carried:
        parts.append("\n".join(carried[-2:]))
    if digest:
        parts.append(digest)
    compacted = head + [{"role": "user", "content": "\n".join(parts)}] + kept
    return compacted, len(dropped), before, conversation_tokens(compacted)


def _elide_tool_content(content: str) -> str:
    if "[elided" in content or len(content) <= 240:
        return content
    meta, _, body = content.partition("\n")
    body = " ".join(body.split())
    return f"{meta[:90]} | {body[:160]} … [elided {len(content)} chars]"


def _elide_assistant_content(content: str) -> str:
    if "[elided" in content or len(content) <= 240:
        return content
    return content[:220].rstrip() + " … [elided]"


def elide_stale_messages(
    messages: list[dict], *, keep_recent_turns: int | None = None
) -> tuple[list[dict], int]:
    """软阈值降级：保留最近 N 轮原文，更早的 assistant 文本/工具输出压成一行。

    只改消息内容，不改 tool_call id 与消息顺序，因此不会破坏 DeepSeek 的
    tool_calls 组形状；幂等，重复执行不会继续缩短。
    """
    keep = KEEP_RECENT_TURNS if keep_recent_turns is None else int(keep_recent_turns)
    msgs = [dict(message) for message in messages]
    assistant_indexes = [index for index, msg in enumerate(msgs) if msg.get("role") == "assistant"]
    if keep <= 0 or len(assistant_indexes) <= keep:
        return msgs, 0
    boundary = assistant_indexes[-keep]
    start = min(2, len(msgs))
    elided = 0
    for index in range(start, boundary):
        message = msgs[index]
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "tool":
            replacement = _elide_tool_content(content)
        elif role == "assistant":
            replacement = _elide_assistant_content(content)
        elif role == "user":
            if content.startswith(("[context trimmed", "[HARNESS STATE")):
                continue
            replacement = content if len(content) <= 400 else content[:380].rstrip() + " …"
        else:
            continue
        if replacement != content:
            msgs[index] = {**message, "content": replacement}
            elided += 1
    return msgs, elided


def emergency_compact(messages: list[dict]) -> list[dict]:
    """上下文溢出 400 的最后手段：压到 20k token 并逐条封顶。"""
    compacted, *_ = compact_history(messages, target_tokens=EMERGENCY_TOKENS, summary_limit=8)
    if not compacted:
        return []
    return [compacted[0]] + [
        _cap_message(message, EMERGENCY_MSG_TOKENS) for message in compacted[1:]
    ]


_CONTEXT_OVERFLOW_RE = re.compile(
    r"(?i)(context[ _-]?length|maximum context|too many tokens|token limit|"
    r"input is too long|reduce the length|request too large|prompt is too long)"
)


def is_context_overflow_error(exc: BaseException) -> bool:
    return bool(_CONTEXT_OVERFLOW_RE.search(str(exc)))


# ------------------------------------------------------- tool output budget ---
_SPILL_SEQ = itertools.count(1)
_HTML_HINT_RE = re.compile(r"(?is)<(?:!doctype|html|head|body|div|table|form|script|style)\b")
_HTML_DROP_RE = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1>")
_HTML_TAG_RE = re.compile(r"(?s)<[^>]+>")


def condense_html(text: str) -> str:
    """HTML 去标签/脚本/样式，仅用于上下文预览；原始输出始终落盘可 grep。"""
    if not _HTML_HINT_RE.search(text[:50000]) and text.count("<") < 20:
        return text
    text = _HTML_DROP_RE.sub("", text)
    text = _HTML_TAG_RE.sub("\n", text)
    text = html_lib.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _spill_dir() -> Path:
    if SOLVER_TASK_PATH:
        base = Path(SOLVER_TASK_PATH).parent / "out" / "tool_outputs"
    else:
        base = Path(cfg("SOLVER_SPILL_DIR", "/tmp/tsec-tool-outputs"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def _write_spill(text: str, label: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(label)).strip("-")[:60] or "tool"
    name = f"{os.getpid()}-{int(time.time())}-{next(_SPILL_SEQ):03d}-{safe}.log"
    path = _spill_dir() / name
    path.write_text(text, encoding="utf-8", errors="replace")
    _prune_spills(path.parent)
    return path


def _prune_spills(base: Path, max_files: int = 200, max_bytes: int = 200 * 1024 * 1024) -> None:
    """限制 out/tool_outputs 体积：保留最新文件，删最旧。"""
    try:
        files = sorted(base.glob("*.log"), key=lambda item: item.stat().st_mtime)
        total = sum(item.stat().st_size for item in files)
    except OSError:
        return
    while files and (len(files) > max_files or (total > max_bytes and len(files) > 1)):
        old = files.pop(0)
        with contextlib.suppress(OSError):
            total -= old.stat().st_size
            old.unlink()


def prepare_tool_output(
    text: str, *, label: str = "tool", turn_budget_left: int | None = None
) -> str:
    """把工具输出压到上下文预算内：大输出落盘，仅回预览 + 文件路径。

    `turn_budget_left` 是本轮剩余输出字符预算；预算耗尽时只回一行指针，
    保证一轮多个 tool_call 也不会把上下文打爆。
    """
    text = str(text or "")
    if not text:
        return ""
    budget = TURN_OUTPUT_BUDGET if turn_budget_left is None else max(0, int(turn_budget_left))
    if len(text) <= SPILL_CHARS and len(text) <= budget:
        return text
    try:
        path = _write_spill(text, label)
    except OSError:
        return truncate(text, limit=min(OUTPUT_LIMIT, max(200, budget)))
    preview_budget = min(SPILL_PREVIEW_CHARS, budget)
    if preview_budget < 200:
        return f"[output spilled: {len(text)} chars -> {path}; per-turn output budget exhausted]"
    body = condense_html(text)
    if len(body) <= preview_budget:
        preview = body
    else:
        head_chars = max(120, preview_budget * 2 // 3)
        tail_chars = max(0, preview_budget - head_chars)
        preview = body[:head_chars]
        if tail_chars:
            preview += "\n...[spilled]...\n" + body[-tail_chars:]
    return (
        f"[output spilled: {len(text)} chars -> {path}]\n"
        f"{preview}\n[full output in the file; grep/sed it for more]"
    )


# --------------------------------------------------------- context logging ---
def _one_line(value: object) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return " ".join(str(value).split())


def render_prior_items(title: str, items: list, *, limit: int = 15, item_chars: int = 160) -> str:
    """把 prior 列表渲染成有界的单行条目；优先保留最近的条目。

    旧实现 `json.dumps(items)[:4000]` 会按字节截断（可能切在条目中间），
    且早期噪声与最新线索同等占位。
    """
    if not isinstance(items, list):
        items = [items]
    values = [_one_line(item)[:item_chars] for item in items]
    values = [value for value in values if value]
    shown = values[-limit:]
    lines = [f"# {title}"]
    lines.extend(f"- {value}" for value in shown)
    omitted = len(values) - len(shown)
    if omitted > 0:
        lines.append(f"- ... {omitted} older entries omitted")
    return "\n".join(lines)


def log_context_event(record: dict) -> None:
    """每轮上下文观测（out/context.jsonl）；无 workspace 时静默跳过。"""
    if not SOLVER_TASK_PATH:
        return
    path = Path(SOLVER_TASK_PATH).parent / "out" / "context.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


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
_DECOY_MARKERS = (
    "test_flag",
    "dummy",
    "placeholder",
    "not_a_real_flag",
    "for_development",
    "changeme",
    "sample_flag",
    "fake_flag",
)
_PLACEHOLDER_BODIES = frozenset(
    {"...", "<...>", "flag", "example", "your_flag_here", "your_flag", "xxx", "redacted"}
)


def is_plausible_flag(flag: str) -> bool:
    """拒绝占位符/示例 flag（FLAG{...}、FLAG{<flag>}、FLAG{xxx}）。"""
    if "{" not in flag or "}" not in flag:
        return False
    body = flag.split("{", 1)[1].rsplit("}", 1)[0].strip()
    if not body or body.lower() in _PLACEHOLDER_BODIES:
        return False
    if "<" in body or ">" in body:
        return False
    # 正则/代码片段（如 flag{[^}）不是 flag
    if any(ch in body for ch in "[]^()|\\*+?"):
        return False
    if any(marker in body.lower() for marker in _DECOY_MARKERS):
        return False
    return not set(body) <= set(".*-_ ")


_reported: set[str] = set()


def find_flags(text: str, include_generic: bool = False) -> list[str]:
    found: list[str] = []
    for pattern in NAMED_FLAG_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(0)
            if not is_plausible_flag(value):
                continue
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


def write_result(status: str, evidence: str = "", next_idea: str = "") -> None:
    """编排器模式收尾：写 out/result.json（编排器不信任它，仅用于日志/回灌）。"""
    if not ORCH_MODE:
        return
    result = {
        "status": status,
        "candidates": sorted(_reported),
        "evidence": evidence,
        "tokens_used": TOTALS.get("prompt", 0) + TOTALS.get("completion", 0),
        "tools_used": [
            {"name": name, "calls": count} for name, count in sorted(TOOL_USAGE.items()) if count
        ],
        "next_idea": next_idea,
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
    if PERSISTENT_SHELL and not CONTAINER:
        shell_rule = (
            "- Each run_command shares ONE persistent bash session: shell variables and cwd "
            "persist across calls. A command that times out restarts the session and discards "
            "them, so write critical values (tokens, cookies, session ids) to files "
            "(e.g. `$TMPDIR/sas`) as well.\n"
            "- stdin is closed (/dev/null): never run interactive tools (nc/ftp/mysql REPLs). "
            "Pass input via arguments, heredocs, or files."
        )
    else:
        shell_rule = (
            "- Each run_command is a FRESH shell: shell variables and cwd do NOT persist. Write "
            "reusable values (SAS tokens, cookies, session ids) to files (e.g. "
            "`$TMPDIR/sas`) and read them in later commands.\n"
            "- stdin is closed: never run interactive tools; pass input via arguments or files."
        )
    contract = f"""
# OPERATING CONTRACT
You drive a lean Linux box through four tools:
- run_command: shell (bash/python3), non-interactive, bounded.
- recon: nmap host scan / web headers+tech / capped 404-filtered dir-bust (JSON).
- rea: Ghidra evidence — `inspect` a local binary or `decompile` one procedure.
- kb: offline security knowledge base (payloads, CVE notes, SOP checklists). Query it before guessing.
- Preinstalled: r2, analyzeHeadless, jadx, feroxbuster, nuclei (templates bundled), ysoserial,
  phpggc, jwt_tool, SSTImap, enum4linux-ng, smbmap, certipy, mysql, psql. Prefer these over re-implementing.

Hard rules:
- No internet/installs/OOB; only the target(s) and LLM API are reachable.
- Target is preconfigured. First timeout = cold start: retry once after ~20s, else change vector. Don't debug VPN/DNS.
- Bound every network command (`curl -m 5`, `timeout 30`). One high-information command beats many small ones.
- JS/CSS/font URLs and JS expressions are NOT assets/credentials; verify with a real login/200.
- 127.0.0.1/localhost URLs in HTML/JS are server-side references; you cannot reach them.
- Never scan `/`; search /app, /root, or your workspace.
- Work on the target and the services reachable from it.
- Scratch: use $TMPDIR (per-task). Absolute /tmp paths are blocked.
- Target 5xx = warming up: retry later, don't call it broken.
{shell_rule}
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
_FAIL_RE = re.compile(
    r"(?i)(404|403|401|500|error|denied|forbidden|refused|timed out|timeout|not found)"
)


def _render_relay(prior: dict) -> str:
    """把上一片的历史压成紧凑 relay 协议：已确认资产/凭据、成功路径、不要重复。"""
    history = str(prior.get("history") or "")
    failed: list[str] = []
    worked: list[str] = []
    for line in history.splitlines():
        head, sep, rest = line.partition(" => ")
        if not sep or not head.strip().startswith("$"):
            continue
        meta, _, body = rest.partition(" | ")
        if _FAIL_RE.search(body) or "exit_code=0" not in meta:
            failed.append(head.strip()[:160])
        elif "HTTP/1.1 200" in body or "HTTP/1.1 302" in body or "password" in body.lower():
            worked.append(head.strip()[:160])
    assets = [str(item) for item in (prior.get("assets") or [])][:12]
    credentials = [str(item) for item in (prior.get("credentials") or [])][:8]
    lines = ["# RELAY PROTOCOL (from previous slices — do not redo)"]
    if assets:
        lines.append("assets: " + ", ".join(assets))
    if credentials:
        lines.append("credentials: " + ", ".join(credentials))
    if worked:
        lines.append("worked: " + " | ".join(worked[-8:]))
    if failed:
        lines.append("do NOT repeat (failed): " + " | ".join(failed[-10:]))
    return "\n".join(lines) if len(lines) > 1 else ""


def setup_orch_mode() -> None:
    """编排器模式：task.json 注入目标与上下文；时间片截断自 SOLVER_DEADLINE_EPOCH。"""
    global TARGET_INFO, TASK_PROMPT, MAX_MINUTES, DEADLINE_EPOCH, TASK
    task = load_task()
    TASK = task
    # 镜像里 3 个 solver 共享 /tmp：把 scratch/home 固定到本题 workspace，
    # 避免并发题互相读到 session/token/notes 中间产物。
    ws_root = Path(SOLVER_TASK_PATH).parent
    scratch = ws_root / "out" / "tmp"
    home = ws_root / "home"
    scratch.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = str(scratch)
    os.environ["HOME"] = str(home)
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
    relay = _render_relay(prior)
    limits = {
        "assets": 20,
        "explored": 15,
        "open_leads": 15,
        "credentials": 20,
        "findings": 15,
    }
    for key, title in (
        ("assets", "Confirmed assets"),
        ("explored", "Already explored (do NOT repeat)"),
        ("open_leads", "Open leads"),
        ("credentials", "Confirmed credentials"),
        ("findings", "Findings (versions/endpoints)"),
    ):
        items = prior.get(key) or []
        if items:
            parts.append(render_prior_items(title, items, limit=limits[key]))
    observer = prior.get("observer")
    if isinstance(observer, dict) and observer:
        lines = ["# OBSERVER (advisory; new evidence beats this)"]
        if observer.get("hypothesis"):
            lines.append(f"HYPOTHESIS: {observer['hypothesis']}")
        if observer.get("confirmed"):
            lines.append("CONFIRMED:")
            lines.extend(f"- {item}" for item in observer["confirmed"][:6])
        if observer.get("lock"):
            lines.append(f"LOCK: {observer['lock']}")
        if observer.get("angles"):
            lines.append("ANGLES:")
            lines.extend(f"- {item}" for item in observer["angles"][:5])
        if observer.get("tension"):
            lines.append("TENSION:")
            lines.extend(f"- {item}" for item in observer["tension"][:2])
        parts.append("\n".join(lines))
    history = str(prior.get("history") or "").strip()
    if history:
        # relay 已给出 worked/failed 摘要时，history 只留最近一小段，
        # 避免两份近似内容重复占上下文。
        history_budget = 2000 if relay else 4000
        parts.append("# Previous attempt (harness state)\n" + history[-history_budget:])
    if relay:
        parts.append(relay)
    previous = str(prior.get("previous_workspace") or "").strip()
    if previous:
        parts.append(
            "# Previous attempt workspace (files still on disk — reuse, do not redo)\n" + previous
        )
        carried = [str(item) for item in (prior.get("carried_files") or [])][:20]
        if carried:
            parts.append(
                "# Carried files (read or grep these first)\n"
                + "\n".join(f"- {f}" for f in carried)
            )
        parts.append(
            "The container may be a new instance: reuse scripts/notes/methodology, "
            "but re-check host/port and credentials before replaying."
        )
    try:
        import skills

        target = str(targets[0]) if targets else ""
        skill = skills.match(
            " ".join([str(task.get("description") or ""), *targets])
        ) or skills.generic_for(
            str(task.get("description") or ""), str(targets[0]) if targets else ""
        )
        if skill:
            parts.append(skills.render(skill, target))
        if int(task.get("flag_count", 1)) > int(task.get("correct_flag_count", 0)):
            parts.append(
                "# MULTI-FLAG TRACKER\n"
                f"Remaining flags: {int(task.get('flag_count', 1)) - int(task.get('correct_flag_count', 0))}. "
                "After each flag, do not stop: list remaining attack surfaces, pivot to a new "
                "credential/port/service, and verify each flag via the exploit that produced it. "
                "Never resubmit old workspace flag files."
            )
    except Exception:  # noqa: BLE001 - skill 是增强，不是必需
        pass
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
