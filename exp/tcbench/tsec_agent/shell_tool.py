"""异步（background）shell 工具。

设计要点：
- 所有命令默认在后台启动，立即返回 job_id；模型按需通过 wait/status 拉取输出。
- 为减少轮次浪费，start 时允许一个很小的同步等待窗口（默认 1.5s），
  命令若在窗口内结束则直接带回完整输出。
- 每个 job 的 stdout/stderr 合并写入临时文件，读取时做大小截断。
- 支持 kill；进程超时自动杀死。
"""

from __future__ import annotations

import os
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


def dispatch_shell(tool_args: dict) -> str:
    """把模型给出的 tool arguments 路由到 ShellManager，返回给模型的文本。"""
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
        return f"error: unknown action {action!r}"
    except Exception as e:  # 兜底，绝不让工具异常打断主循环
        return f"error: shell tool failed: {type(e).__name__}: {e}"


SHELL = ShellManager()
