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

    def size(self) -> int:
        """当前输出文件字节数（用于增量/边沿等待）。"""
        try:
            return os.path.getsize(self.out_path)
        except OSError:
            return 0


def _image_size(data: bytes):
    """从文件头解析图片尺寸（PNG/JPEG/GIF/WebP/BMP），零依赖。返回 (w, h) 或 (None, None)。"""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return (int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"))
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return (int.from_bytes(data[6:8], "little"),
                    int.from_bytes(data[8:10], "little"))
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            if data[12:16] == b"VP8X":
                return (1 + int.from_bytes(data[24:27], "little"),
                        1 + int.from_bytes(data[27:30], "little"))
            if data[12:16] == b"VP8 ":
                return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                        int.from_bytes(data[28:30], "little") & 0x3FFF)
            if data[12:16] == b"VP8L":
                b0, b1, b2, b3 = data[21:25]
                return (1 + (((b1 & 0x3F) << 8) | b0),
                        1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6)))
        if data[:2] == b"BM" and len(data) >= 26:
            return (int.from_bytes(data[18:22], "little"),
                    abs(int.from_bytes(data[22:26], "little")))
        if data[:2] == b"\xff\xd8":  # JPEG：扫段找 SOF
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seglen = int.from_bytes(data[i + 2:i + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    return (int.from_bytes(data[i + 7:i + 9], "big"),
                            int.from_bytes(data[i + 5:i + 7], "big"))
                i += 2 + seglen
    except Exception:
        pass
    return None, None


def _shrink(path_in: str, cap_bytes: int):
    """超过大小上限时压图：agent venv 没有 PIL，但系统 python3 有 python3-pil。

    返回压缩后（JPEG）的临时文件路径，失败返回 None。
    """
    script = (
        "import sys, os\n"
        "from PIL import Image\n"
        "src, dst, cap = sys.argv[1], sys.argv[2], int(sys.argv[3])\n"
        "im = Image.open(src)\n"
        "im.thumbnail((1536, 1536))\n"
        "if im.mode not in ('RGB', 'L'):\n"
        "    im = im.convert('RGB')\n"
        "q = 85\n"
        "while True:\n"
        "    im.save(dst, 'JPEG', quality=q, optimize=True)\n"
        "    if os.path.getsize(dst) <= cap or q <= 30:\n"
        "        break\n"
        "    q -= 15\n"
    )
    fd, dst = tempfile.mkstemp(prefix="view_", suffix=".jpg")
    os.close(fd)
    try:
        r = subprocess.run(["python3", "-c", script, path_in, dst, str(cap_bytes)],
                           capture_output=True, timeout=25)
        if r.returncode == 0 and os.path.getsize(dst) <= cap_bytes:
            return dst
    except Exception:
        pass
    try:
        os.unlink(dst)
    except OSError:
        pass
    return None


def _ocr(path: str) -> str:
    """文本模式（网关不支持图像）时的兜底：tesseract 英文 OCR。"""
    try:
        r = subprocess.run(["tesseract", path, "stdout", "-l", "eng"],
                           capture_output=True, timeout=25, text=True)
        txt = (r.stdout or "").strip()
        if txt:
            return txt[:4000]
    except Exception:
        pass
    return ""


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
        timeout: float | None = None,
        wait_ms: int | None = None,
        wait_sec: float | None = None,
        out_file: str | None = None,
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

        # job 上限：满了拒绝新命令，避免后台 job 堆成僵尸
        with self._lock:
            active = [j for j in self._jobs.values() if not j.finished]
        if len(active) >= CFG.shell_max_jobs:
            ids = ", ".join(f"{j.job_id}({int(time.time() - j.started_at)}s)"
                            for j in active)
            return (f"error: 后台 job 已满（{len(active)}/{CFG.shell_max_jobs}）：{ids}\n"
                    f"先 action=list 看进度，用 wait/wait_any 收结果，"
                    f"或 kill 掉不需要的 job，再启动新命令。")
        # 硬上限：到点整组 kill（输出已落盘，杀掉不丢数据）；
        # 未传用配置默认 1800s；显式传 0/负数 = 永不杀
        if timeout is None:
            timeout = CFG.shell_hard_sec

        job_id = uuid.uuid4().hex[:8]
        if out_file:
            path = out_file if os.path.isabs(out_file) else os.path.join(cwd, out_file)
            try:
                os.makedirs(os.path.dirname(path) or cwd, exist_ok=True)
                out_path = path          # 直接写到 workdir，模型可随时 tail
            except OSError:
                out_path = self._new_log(job_id)
        else:
            out_path = self._new_log(job_id)
        env = os.environ.copy()
        env.update(CFG.extra_env)

        try:
            popen = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdout=open(out_path, "wb"),
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
        # wait_sec：工具替模型决定「什么时候回来叫它」——
        # 命令结束 / 攒够一批新输出（>2KB）/ 到点，三者先到先返回
        if wait_sec and not job.finished:
            limit = time.time() + max(0.0, min(float(wait_sec), 300.0))
            base = job.size()
            while not job.finished and time.time() < limit:
                if job.size() > base + 2048:
                    break
                time.sleep(0.1)
        payload = self._status_payload(job)
        extra = []
        with self._lock:
            others = [j.job_id for j in self._jobs.values()
                      if not j.finished and j.job_id != job.job_id]
        if others:
            extra.append(f"后台还有 {len(others)} 个 job 在跑 ({', '.join(others)})："
                         f"发完这条就去做别的事，要同时盯多个用 action=wait_any")
        else:
            extra.append("命令已在后台：先去做别的事，需要时用 wait/status 读结果"
                         "（支持 offset 分页）；长命令建议 out_file 落盘")
        return payload + "\n" + "\n".join(extra)

    @staticmethod
    def _new_log(job_id: str) -> str:
        fd, path = tempfile.mkstemp(prefix=f"shell_{job_id}_", suffix=".log")
        os.close(fd)
        return path

    # ---- 动作：等待（边沿触发：结束 / 攒够新输出 / 超时，先到先返回） ----
    def wait(self, job_id: str, timeout: float = 30.0,
             offset: int | None = None, lines: int | None = None) -> str:
        job = self._get(job_id)
        if job is None:
            return f"error: unknown job_id {job_id}"
        deadline = time.time() + max(0.0, min(timeout, 300.0))
        base = int(offset) if offset is not None else job.size()
        while not job.finished and time.time() < deadline:
            if job.size() > base + 2048:   # 已攒够一批新输出 → 不必等到底
                break
            time.sleep(0.1)
        return self._status_payload(job, offset=offset, lines=lines)

    # ---- 动作：同时等多个 job，任一有动静就返回 ----
    def wait_any(self, job_ids, timeout: float = 60.0,
                 lines: int | None = None) -> str:
        if isinstance(job_ids, str):
            job_ids = [job_ids]
        ids = [j for j in (job_ids or []) if j]
        if not ids:
            return "error: `job_ids` is required for action=wait_any (array of job ids)"
        pairs = [(jid, self._get(jid)) for jid in ids]
        missing = [jid for jid, jb in pairs if jb is None]
        pairs = [(jid, jb) for jid, jb in pairs if jb is not None]
        if not pairs:
            return f"error: unknown job_ids {ids}"
        base = {jid: jb.size() for jid, jb in pairs}
        deadline = time.time() + max(0.0, min(float(timeout), 300.0))
        while time.time() < deadline:
            if any(jb.finished for _, jb in pairs):
                break
            if any(jb.size() > base[jid] + 512 for jid, jb in pairs):
                break
            time.sleep(0.1)
        parts = [self._status_payload(jb, lines=lines) for _, jb in pairs]
        tail = "\n未结束的 job 继续在后台跑；继续盯可再用 wait_any 或单独 wait。"
        if missing:
            tail += f"\n(已忽略未知 job_id: {', '.join(missing)})"
        return "任一 job 有新输出或结束即返回\n" + "\n---\n".join(parts) + tail

    # ---- 动作：查看状态/输出（不阻塞，支持分页） ----
    def status(self, job_id: str, offset: int | None = None,
               lines: int | None = None) -> str:
        job = self._get(job_id)
        if job is None:
            return f"error: unknown job_id {job_id}"
        return self._status_payload(job, offset=offset, lines=lines)

    # ---- 动作：列出所有 job ----
    def list(self) -> str:
        with self._lock:
            jobs = list(self._jobs.values())
        if not jobs:
            return "no jobs"
        lines = []
        for j in jobs[-20:]:
            state = (f"done rc={j.exit_code}" if j.finished
                     else f"RUN {int(time.time() - j.started_at)}s/{j.timeout or 0:.0f}s")
            mark = "*" if not j.finished else " "
            lines.append(f"{mark}{j.job_id}  {state:16} out={j.size():>8}B  "
                         f"{j.command[:72]}")
        n_run = sum(1 for j in jobs if not j.finished)
        return ("\n".join(lines)
                + f"\n(* = 在跑，{n_run}/{CFG.shell_max_jobs}；wait/status 读输出（offset 分页），"
                  f"wait_any 同时等多个，kill 停止)")

    # ---- 动作：杀掉 ----
    def kill(self, job_id: str, lines: int | None = None) -> str:
        job = self._get(job_id)
        if job is None:
            return f"error: unknown job_id {job_id}"
        job.kill(reason="user")
        time.sleep(0.2)
        payload = self._status_payload(job, lines=lines or 40)
        return payload + (f"\nnote: 已终止；完整输出已落盘 {job.out_path}"
                          f"（可 run \"tail -n 200 {job.out_path}\" 继续读）")

    # ---- 动作：读图（多模态，把图片本体交给模型看） ----
    def view(self, path: str, detail: str | None = None):
        """返回 [text, image_url] 两段；网关不支持图像时自动降级为纯文本（OCR+元数据）。"""
        detail = detail if detail in ("low", "high") else "low"
        p = path if os.path.isabs(path) else os.path.join(get_task_cwd() or self._cwd, path)
        if not os.path.isfile(p):
            return f"error: 图片不存在或不是普通文件: {p}"
        ext = os.path.splitext(p)[1].lower()
        if ext not in _IMAGE_EXT:
            return ("error: 不支持的图片格式 " + (ext or "(无扩展名)") + "；支持 "
                    + ", ".join(sorted(_IMAGE_EXT)) + "。先用 file 确认类型。")
        try:
            with open(p, "rb") as f:
                data = f.read()
        except OSError as e:
            return f"error: 读取图片失败: {e}"
        if not data:
            return f"error: 空文件: {p}"
        w, h = _image_size(data)
        dims = f"{w}x{h}" if w else "尺寸未知"
        mime = _IMAGE_EXT[ext]
        cap = max(64, CFG.vision_max_kb) * 1024
        note = ""
        if len(data) > cap:
            shrunk = _shrink(p, cap)
            if not shrunk:
                return (f"error: {p} 有 {len(data) // 1024}KB，超过上限 "
                        f"{CFG.vision_max_kb}KB 且压缩失败。请先自行缩小/裁剪图片再 view。")
            p = shrunk
            with open(shrunk, "rb") as f:
                data = f.read()
            mime = "image/jpeg"
            w, h = _image_size(data)
            dims = f"{w}x{h}" if w else dims
            note = f"（原图超限，已自动压缩到 ≤{CFG.vision_max_kb}KB）"
        header = f"[image] {p}  {dims}  {len(data)}B  {mime}  detail={detail} {note}"
        if CFG.vision == "off" or CFG.vision_ok is False:
            ocr = _ocr(p)
            return (
                header + "\n当前为文本模式（AGENT_VISION=off 或网关已拒图），没有图片本体："
                + (f"\nOCR(tesseract eng):\n{ocr}\n" if ocr else "\nOCR 无输出。\n")
                + "请改用 shell 侧分析：file/xxd/strings/exiftool、tesseract -l eng、"
                  "系统 python3 的 PIL（尺寸/直方图/像素采样），或先裁剪关键区域。"
            )
        import base64
        url = "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")
        return [
            {"type": "text",
             "text": header + "\n下面是图片本体，直接看图作答；需要更细可传 detail=high，"
                              "或先用 shell 裁剪/缩放。"},
            {"type": "image_url", "image_url": {"url": url, "detail": detail}},
        ]

    def _get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    @staticmethod
    def _read_page(job: Job, offset: int | None, lines: int | None) -> tuple:
        """读输出分页：offset 给定 → 从该字节续读；否则 → 末尾 N 行（默认 200）。"""
        try:
            total = job.size()
            with open(job.out_path, "rb") as f:
                if offset is None:
                    want = max(1, min(int(lines or 200), 2000))
                    start = max(0, total - max(want * 160, 8192))
                    f.seek(start)
                    data = f.read()
                    txt = data.decode("utf-8", errors="replace").splitlines()
                    page = "\n".join(txt[-want:])
                    omitted = total - len(data)
                    more = (f"（更早还有 {omitted} 字节，offset=0 可从头分页读）"
                            if omitted else "")
                    return page, total, more
                off = max(0, int(offset))
                f.seek(off)
                data = f.read(CFG.shell_max_output)
                page = data.decode("utf-8", errors="replace")
                nxt = off + len(data)
                more = (f"（后面还有 {total - nxt} 字节，offset={nxt} 续读）"
                        if nxt < total else "")
                return page, nxt, more
        except OSError:
            return "", 0, ""

    def _status_payload(self, job: Job, offset: int | None = None,
                        lines: int | None = None) -> str:
        if job.finished:
            state = f"finished exit_code={job.exit_code}"
            if job.killed and job.exit_code not in (0, None):
                state += " (killed: timeout or manual kill)"
        else:
            elapsed = time.time() - job.started_at
            state = f"running elapsed={elapsed:.1f}s (hard limit {job.timeout or 0:.0f}s)"
        page, nxt, more = self._read_page(job, offset, lines)
        header = (f"[job {job.job_id}] {state} out={job.size()}B\n"
                  f"log: {job.out_path}\ncmd: {job.command}")
        if not page:
            return f"{header}\noutput: (empty)"
        pos = f"from_offset={offset}" if offset is not None else "tail"
        tail = f"\nnext_offset={nxt}" + (f" {more}" if more else "")
        return f"{header}\noutput[{pos}]:\n{page}{tail}"


SHELL_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": (
            "Run a shell command on the local machine. Commands run ASYNCHRONOUSLY: the "
            "call returns a job_id quickly (commands finishing in the ~1.5s sync window "
            "return their full output immediately). `command` is the only field you must "
            "send — action is optional and defaults to run (sending only job_id means "
            "status). Waiting is edge-triggered: wait returns as soon as the command "
            "FINISHES, or has accumulated >=2KB of NEW output, or the timeout hits — "
            "whichever comes first, so it never blocks the full timeout for nothing. "
            "On run, pass wait_sec to let the tool decide when to wake you; meanwhile go "
            "do other work instead of idling. Read output incrementally: each response "
            "carries next_offset, continue with offset=next_offset (or lines for the "
            "tail). For long scans pass out_file to write straight into your workdir so "
            "the output survives kills and you can tail it. Jobs are killed at the hard "
            "limit (default 1800s); kill sooner if stuck — output stays on disk. Max 8 "
            "concurrent jobs (new run is rejected until you wait/kill). "
            "Multimodal: to look at an image (screenshot, chart, captcha, photo) call "
            "action=view with path — the picture itself is returned to you, so you can "
            "read/understand it directly; if the gateway rejects images it degrades to "
            "text (OCR + metadata) automatically. "
            "Actions: run (default), wait, wait_any (block until ANY of several jobs "
            "makes progress), status, kill, list, view. "
            "Environment already contains BENCHMARK_TOKEN and BENCHMARK_BASE_URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["run", "wait", "wait_any", "status", "kill", "list", "view"],
                    "description": "optional (default run); wait=block for progress; "
                                   "wait_any=block until any of job_ids progresses; "
                                   "status=non-blocking peek; kill=stop; list=recent jobs; "
                                   "view=show an image to yourself (multimodal)",
                },
                "command": {"type": "string", "description": "shell command (action=run)"},
                "path": {
                    "type": "string",
                    "description": "image file path (action=view), absolute or relative to cwd",
                },
                "detail": {
                    "type": "string",
                    "enum": ["low", "high"],
                    "description": "action=view: image detail level (default low)",
                },
                "job_id": {"type": "string", "description": "job id (wait/status/kill)"},
                "job_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "job ids to watch together (action=wait_any)",
                },
                "cwd": {"type": "string", "description": "working directory (action=run)"},
                "out_file": {
                    "type": "string",
                    "description": "action=run: write output straight to this path "
                                   "(absolute or relative to cwd), e.g. nmap.log in your "
                                   "workdir — survives kills, you can tail it later",
                },
                "wait_sec": {
                    "type": "number",
                    "description": "action=run: block up to N sec (max 300) so this single "
                                   "call already returns real output when the command "
                                   "finishes or streams >=2KB new output. Use it instead "
                                   "of calling wait right after run. Try 60 for scans.",
                },
                "wait_ms": {
                    "type": "integer",
                    "description": "action=run: initial sync window in ms (default 1500, max 30000)",
                },
                "timeout_sec": {
                    "type": "number",
                    "description": "action=run: hard kill after N sec (default 1800, 0=never); "
                                   "action=wait/wait_any: max block N sec (wait default 30, "
                                   "wait_any default 60, max 300)",
                },
                "offset": {
                    "type": "integer",
                    "description": "wait/status: byte offset to read output from — use the "
                                   "next_offset returned earlier to page without re-reading",
                },
                "lines": {
                    "type": "integer",
                    "description": "wait/status/wait_any/kill: number of trailing lines to "
                                   "return (default 200)",
                },
            },
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
KNOWN_ACTIONS = ("run", "wait", "wait_any", "status", "kill", "list", "view")
_IMAGE_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".tif": "image/tiff", ".tiff": "image/tiff",
}
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
    # action 可省略：只有 job_id 没 command → 当作 status（省得模型补 action）
    if ("action" not in tool_args and not tool_args.get("command")
            and tool_args.get("job_id")):
        tool_args = dict(tool_args, action="status")

    def _int(v):
        try:
            return None if v is None else int(v)
        except (TypeError, ValueError):
            return None

    def _float(v, default):
        try:
            return default if v is None else float(v)
        except (TypeError, ValueError):
            return default

    action = tool_args.get("action", "run")
    try:
        if action == "run":
            command = tool_args.get("command")
            if not command:
                return "error: `command` is required for action=run"
            return SHELL.run(
                command=command,
                cwd=tool_args.get("cwd"),
                timeout=_float(tool_args.get("timeout_sec"), None),  # 缺省→配置默认 1800s
                wait_ms=_int(tool_args.get("wait_ms")),
                wait_sec=_float(tool_args.get("wait_sec"), None),
                out_file=tool_args.get("out_file"),
            )
        if action == "wait":
            job_id = tool_args.get("job_id")
            if not job_id:
                return "error: `job_id` is required for action=wait"
            return SHELL.wait(job_id, timeout=_float(tool_args.get("timeout_sec"), 30.0),
                              offset=_int(tool_args.get("offset")),
                              lines=_int(tool_args.get("lines")))
        if action == "wait_any":
            ids = tool_args.get("job_ids") or tool_args.get("job_id")
            return SHELL.wait_any(ids, timeout=_float(tool_args.get("timeout_sec"), 60.0),
                                  lines=_int(tool_args.get("lines")))
        if action == "status":
            job_id = tool_args.get("job_id")
            if not job_id:
                return "error: `job_id` is required for action=status"
            return SHELL.status(job_id, offset=_int(tool_args.get("offset")),
                                lines=_int(tool_args.get("lines")))
        if action == "kill":
            job_id = tool_args.get("job_id")
            if not job_id:
                return "error: `job_id` is required for action=kill"
            return SHELL.kill(job_id, lines=_int(tool_args.get("lines")))
        if action in ("view", "image"):
            target = tool_args.get("path") or tool_args.get("file") or tool_args.get("command")
            if not target:
                return "error: `path` is required for action=view"
            return SHELL.view(str(target), detail=tool_args.get("detail"))
        if action == "list":
            return SHELL.list()
        return (f"error: unknown action {action!r}; 请用 "
                f'{{"action":"run","command":"<命令>"}} 或 '
                f'{{"action":"wait","job_id":"<job>"}}（可选 wait_any/status/kill/list）')
    except Exception as e:  # 兜底，绝不让工具异常打断主循环
        return f"error: shell tool failed: {type(e).__name__}: {e}"


SHELL = ShellManager()
