"""异步（background）shell 工具。

设计要点：
- 所有命令默认在后台启动，立即返回 job_id；模型按需通过 wait/status 拉取输出。
- 为减少轮次浪费，start 时允许一个很小的同步等待窗口（默认 1.5s），
  命令若在窗口内结束则直接带回完整输出。
- 每个 job 的 stdout/stderr 合并写入临时文件，读取时做大小截断。
- 支持 kill；进程超时自动杀死。
- **工具参数容错**：模型偶发把参数序列化坏（action 混入 XML 参数片段 / 整段命令、
  JSON 值漏引号）→ `parse_tool_arguments` 自动修复并执行，附修复说明落 events.jsonl
  （`tool_arg_repaired`），避免整轮作废 + 模型反复自查。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field

from config import CFG

# 线程局部"当前任务工作目录"：并行解题时每个任务线程有专属 cwd，互不干扰
_tls = threading.local()


def set_task_cwd(path: str) -> None:
    _tls.cwd = path


def get_task_cwd() -> str | None:
    return getattr(_tls, "cwd", None)


@dataclass
class Job:
    job_id: str
    command: str
    cwd: str
    popen: subprocess.Popen
    out_path: str
    started_at: float
    timeout: float | None
    finished: bool = False
    exit_code: int | None = None
    killed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _watcher: threading.Thread | None = field(default=None, repr=False)

    def _reap(self) -> None:
        try:
            rc = self.popen.wait()
        except Exception:
            rc = -1
        with self._lock:
            self.exit_code = rc
            self.finished = True

    def ensure_watched(self) -> None:
        if self._watcher is not None:
            return
        with self._lock:
            if self._watcher is not None:
                return

            def watch():
                deadline = (self.started_at + self.timeout) if (self.timeout and self.timeout > 0) else None
                while self.popen.poll() is None:
                    if deadline is not None and time.time() > deadline:
                        self.killed = True
                        self.kill(reason="timeout")
                        break
                    time.sleep(0.1)
                # reap：回收子进程，记录退出码
                try:
                    rc = self.popen.wait(timeout=5)
                except Exception:
                    rc = self.popen.poll()
                with self._lock:
                    self.exit_code = rc
                    self.finished = True

            t = threading.Thread(target=watch, daemon=True)
            self._watcher = t
            t.start()

    def kill(self, reason: str = "user") -> None:
        with self._lock:
            if self.finished:
                return
            self.killed = True
        try:
            # 杀整个进程组，避免 curl/python 子进程残留
            os.killpg(os.getpgid(self.popen.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.popen.kill()
            except Exception:
                pass

    def output(self, max_bytes: int = CFG.shell_max_output) -> str:
        try:
            with open(self.out_path, "rb") as f:
                data = f.read()
        except OSError:
            return ""
        if len(data) <= max_bytes:
            return data.decode("utf-8", errors="replace")
        head = data[: max_bytes // 2]
        tail = data[-(max_bytes - max_bytes // 2 - 200):]
        omitted = len(data) - len(head) - len(tail)
        return (
            head.decode("utf-8", errors="replace")
            + f"\n...[output truncated, {omitted} bytes omitted]...\n"
            + tail.decode("utf-8", errors="replace")
        )


class ShellManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._cwd = os.getcwd()

    # ---- 动作：启动（后台） ----
    def run(
        self,
        command: str,
        cwd: str | None = None,
        timeout: float | None = 300.0,
        wait_ms: int | None = None,
    ) -> str:
        cwd = cwd or get_task_cwd() or self._cwd
        if not os.path.isdir(cwd):
            try:
                os.makedirs(cwd, exist_ok=True)
            except OSError:
                cwd = self._cwd
        if wait_ms is None:
            wait_ms = CFG.shell_default_wait_ms
        wait_ms = max(0, min(int(wait_ms), 30_000))

        job_id = uuid.uuid4().hex[:8]
        fd, out_path = tempfile.mkstemp(prefix=f"shell_{job_id}_", suffix=".log")
        os.close(fd)
        env = os.environ.copy()
        env.update(CFG.extra_env)

        try:
            popen = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdout=open(out_path, "ab"),
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,  # 新建会话/进程组，便于整组 kill
            )
        except Exception as e:
            return f"error: failed to start command: {e}"

        job = Job(
            job_id=job_id,
            command=command,
            cwd=cwd,
            popen=popen,
            out_path=out_path,
            started_at=time.time(),
            timeout=timeout,
        )
        with self._lock:
            self._jobs[job_id] = job
        job.ensure_watched()

        # 小窗口同步等待：快速命令直接带完整输出返回，减少一轮交互
        deadline = time.time() + wait_ms / 1000.0
        while time.time() < deadline and not job.finished:
            time.sleep(0.05)
        return self._status_payload(job, brief_running=True)

    # ---- 动作：等待完成 ----
    def wait(self, job_id: str, timeout: float = 30.0) -> str:
        job = self._get(job_id)
        if job is None:
            return f"error: unknown job_id {job_id}"
        deadline = time.time() + max(0.0, min(timeout, 300.0))
        while not job.finished and time.time() < deadline:
            time.sleep(0.1)
        return self._status_payload(job, brief_running=True)

    # ---- 动作：查看状态/输出（不阻塞） ----
    def status(self, job_id: str) -> str:
        job = self._get(job_id)
        if job is None:
            return f"error: unknown job_id {job_id}"
        return self._status_payload(job, brief_running=True)

    # ---- 动作：列出所有 job ----
    def list(self) -> str:
        with self._lock:
            jobs = list(self._jobs.values())
        if not jobs:
            return "no jobs"
        lines = [self._status_payload(j, brief_running=True) for j in jobs[-20:]]
        return "\n".join(lines)

    # ---- 动作：杀掉 ----
    def kill(self, job_id: str) -> str:
        job = self._get(job_id)
        if job is None:
            return f"error: unknown job_id {job_id}"
        job.kill(reason="user")
        time.sleep(0.2)
        return self._status_payload(job, brief_running=True)

    def _get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _status_payload(self, job: Job, brief_running: bool = True) -> str:
        out = job.output()
        if job.finished:
            state = f"finished exit_code={job.exit_code}"
            if job.killed and job.exit_code not in (0, None):
                state += " (killed: timeout or manual kill)"
        else:
            elapsed = time.time() - job.started_at
            state = f"running elapsed={elapsed:.1f}s"
        header = f"[job {job.job_id}] {state}\ncmd: {job.command}"
        if out:
            return f"{header}\noutput:\n{out}"
        return f"{header}\noutput: (empty)"


SHELL_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": (
            "Run a shell command on the local machine. Commands run ASYNCHRONOUSLY in the "
            "background: the call returns quickly with a job_id. Use action='wait' to block "
            "until the command finishes (up to `timeout` seconds) and get its output. "
            "Quick commands (finishing within ~1.5s) return their full output immediately. "
            "Environment already contains BENCHMARK_TOKEN and BENCHMARK_BASE_URL. "
            "Actions: run (start command), wait (block for output), status (peek), "
            "kill (stop), list (show recent jobs)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["run", "wait", "status", "kill", "list"],
                    "description": "run=start command (default); wait=block for completion; "
                                   "status=non-blocking peek; kill=stop; list=recent jobs",
                },
                "command": {"type": "string", "description": "shell command (action=run)"},
                "job_id": {"type": "string", "description": "job id (wait/status/kill)"},
                "cwd": {"type": "string", "description": "working directory (action=run)"},
                "timeout_sec": {
                    "type": "number",
                    "description": "action=run: kill command after N sec (default 300); "
                                   "action=wait: max block N sec (default 30, max 300)",
                },
                "wait_ms": {
                    "type": "integer",
                    "description": "action=run: initial sync wait window in ms (default 1500, max 30000)",
                },
            },
            "required": ["action"],
        },
    },
}


# --- 工具参数容错（模型偶发把参数序列化坏，实测样本） ---
#   1) action 混入 XML 参数片段：run"> + <parameter name="command">...（命令在里面）
#      或 wait"> + <parameter name="job_id">a5daca2b（job_id 在里面）
#   2) action 被写成整条命令：cd /work/...; ffuf ...（没有 command 键）
#   3) action 写成别的工具名：shell（command 正常）
#   4) JSON 值没加引号：job_id: 829f40d3 -> JSON 解析直接失败
# 每条都烧掉整整一轮，且模型收到报错后往往再花几轮自查——所以在工具层直接修复。
KNOWN_ACTIONS = ("run", "wait", "status", "kill", "list")
_PARAM_RE = re.compile(
    r'<parameter\s+name="(\w+)">\s*(.*?)(?:(?=</parameter>)|(?=\s*<parameter\s)|\Z)', re.S)
_LEAD_RE = re.compile(r'^\s*(run|wait|status|kill|list)\b')
_BARE_RE = re.compile(r'(:\s*)([A-Za-z0-9_.\-+/]+)(?=\s*[,}\n])')
_LEAD_STOP = '"' + chr(39) + '>'   # 坏 action 里动作名后面的残渣字符：双引号/单引号/大于号


def _looks_like_command(s: str) -> bool:
    """像 shell 命令而不是动作名：含空白/管道/重定向/变量等，或以路径开头。"""
    return bool(s) and (len(s.split()) > 1
                        or any(c in s for c in ";|&<>$`\n") or s.startswith("/"))


def _lead_action(text):
    """识别 run"/wait"> 之类被截断的动作名前缀，返回 (动作名, 剩余文本) 或 None。"""
    m = _LEAD_RE.match(text)
    if not m:
        return None
    rest = text[m.end():]
    if rest and not (rest[0] in _LEAD_STOP or rest[0].isspace()):
        return None  # 例如 running/runner 这种以动作名开头的普通命令
    return m.group(1), rest.lstrip(' ">')


def _extract_params(text: str) -> dict:
    """抽出 XML 参数片段（开标签 name="x" + 值 [+ 闭合标签]），值可能一直到字符串结尾。"""
    return {m.group(1): m.group(2).strip()
            for m in _PARAM_RE.finditer(text) if m.group(2).strip()}


def _quote_bare_values(raw: str) -> str:
    """给未加引号的 JSON 值补引号（true/false/null 与合法数字保持原样）。"""
    def repl(m):
        tok = m.group(2)
        if tok in ("true", "false", "null"):
            return m.group(0)
        try:
            float(tok)
            return m.group(0)  # 合法数字，不加引号
        except ValueError:
            return m.group(1) + chr(34) + tok + chr(34)
    return _BARE_RE.sub(repl, raw)


def repair_tool_args(tool_args: dict) -> tuple:
    """修复 dict 形式的工具参数（幂等）。返回 (修复后的 args, 修复说明或 None)。

    只在能确定意图时改写：拿不准就原样返回，由 dispatch 报正常错误。
    """
    action = tool_args.get("action", "run")
    if action is None:
        action = "run"
    if isinstance(action, str) and action.strip() in KNOWN_ACTIONS:
        return tool_args, None
    if not isinstance(action, str):
        if tool_args.get("command"):
            out = dict(tool_args)
            out["action"] = "run"
            return out, "action 不是字符串，按 run 执行"
        return tool_args, None
    text = action.strip()
    # 1) action 混入 XML 参数片段
    lead = _lead_action(text)
    if lead:
        name, rest = lead
        params = _extract_params(rest)
        out = dict(tool_args)
        out["action"] = name
        if name == "run":
            cmd = params.get("command") or tool_args.get("command") or rest.strip()
            if cmd:
                out["command"] = cmd
        else:
            job_id = params.get("job_id") or tool_args.get("job_id") or rest.strip()
            if job_id:
                out["job_id"] = job_id
        return out, "action 字段混入 XML 参数片段，已还原为 " + repr(name)
    # 2) action 非法但 command 正常 -> 按 run 执行
    if tool_args.get("command"):
        out = dict(tool_args)
        out["action"] = "run"
        return out, "action 非法但存在 command，已按 run 执行"
    # 3) action 整段是命令 -> run + command=action（其余参数保留）
    if _looks_like_command(text):
        out = dict(tool_args)
        out["action"] = "run"
        out["command"] = action
        return out, "action 字段被写成整条命令，已按 run 执行"
    # 4) 拿不准（如 shell 且无 command）-> 原样返回，报 unknown action
    return tool_args, None


def parse_tool_arguments(raw: str) -> tuple:
    """解析模型给出的 shell 工具参数 JSON，容错修复已知序列化损坏。

    返回 (args, 修复说明或 None)；无法解析返回 (None, 说明)。
    """
    text = (raw or "").strip()
    if not text:
        return None, "工具参数为空"
    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        pass
    if isinstance(data, dict):
        return repair_tool_args(data)
    if isinstance(data, str) and _looks_like_command(data):
        return {"action": "run", "command": data}, "参数是裸命令字符串，已按 run 执行"
    # JSON 解析失败 -> 依次尝试：补引号 / 重建 XML 片段 / 整段当命令
    try:
        fixed = json.loads(_quote_bare_values(text))
        if isinstance(fixed, dict):
            args, note = repair_tool_args(fixed)
            parts = [p for p in ("已为未加引号的 JSON 值补引号", note) if p]
            return args, "; ".join(parts)
    except json.JSONDecodeError:
        pass
    params = _extract_params(text)
    if params:
        lead = _lead_action(text)
        name = (params.pop("action", None)
                or (lead[0] if lead else None)
                or ("run" if "command" in params else None))
        if name in KNOWN_ACTIONS:
            args, note = repair_tool_args(dict({"action": name}, **params))
            parts = [p for p in ("参数被序列化成 XML 片段，已重建", note) if p]
            return args, "; ".join(parts)
    if _looks_like_command(text):
        return {"action": "run", "command": text}, "参数不是 JSON，已整段按命令执行"
    return None, "JSON 参数解析失败且无法修复：" + raw[:160]


def dispatch_shell(tool_args: dict) -> str:
    """把模型给出的 tool arguments 路由到 ShellManager，返回给模型的文本。"""
    try:
        tool_args, _ = repair_tool_args(tool_args or {})
    except Exception:
        pass  # 修复失败按原样走，由下面的分支给出正常错误
    action = tool_args.get("action", "run")
    try:
        if action == "run":
            command = tool_args.get("command")
            if not command:
                return "error: `command` is required for action=run"
            return SHELL.run(
                command=command,
                cwd=tool_args.get("cwd"),
                timeout=tool_args.get("timeout_sec", 300.0),
                wait_ms=tool_args.get("wait_ms"),
            )
        if action == "wait":
            job_id = tool_args.get("job_id")
            if not job_id:
                return "error: `job_id` is required for action=wait"
            return SHELL.wait(job_id, timeout=float(tool_args.get("timeout_sec", 30.0)))
        if action == "status":
            job_id = tool_args.get("job_id")
            if not job_id:
                return "error: `job_id` is required for action=status"
            return SHELL.status(job_id)
        if action == "kill":
            job_id = tool_args.get("job_id")
            if not job_id:
                return "error: `job_id` is required for action=kill"
            return SHELL.kill(job_id)
        if action == "list":
            return SHELL.list()
        return (f"error: unknown action {action!r}; 请用 "
                f'{{"action":"run","command":"<命令>"}} 或 '
                f'{{"action":"wait","job_id":"<job>"}}（可选 status/kill/list）')
    except Exception as e:  # 兜底，绝不让工具异常打断主循环
        return f"error: shell tool failed: {type(e).__name__}: {e}"


SHELL = ShellManager()
