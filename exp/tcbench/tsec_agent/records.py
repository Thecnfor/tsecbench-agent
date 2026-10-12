"""bench 级持久化记录（替代旧 trace 双文件系统）。

布局（每个 bench 一个目录，唯一键 = token，无时间戳）：
    runs/<TOKEN>/
        meta.json          本次 bench 元信息 + 每次运行的起止与结果（runs[] 数组）
        events.jsonl       run 级事件（llm_retry、platform_poll_error、final_summary…）
        tasks.jsonl        题目生命周期（task_start/task_close + 起止时间/耗时/结果/flags）
        tasks/<code>/
            conversation.jsonl   该题完整 LLM 对话：全量、不裁剪、忠实记录

原则：
- 对话忠诚：发给 AI 什么、AI 输出什么，原样落盘（thinking/output/tool_calls/tool result），
  绝不截断。
- 目录挂载即隔离：容器只挂当前 bench 目录，其他 bench 不在挂载内；
  旧 bench 由 archive_runs.sh 归档到 docker 目录外。不做 shell 拦截。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

# 记录根目录：默认 /work（= 容器内当前 bench 挂载点）
RECORDS_DIR = os.environ.get("RECORDS_DIR", "/work")
if not os.path.isdir(RECORDS_DIR):
    RECORDS_DIR = os.getcwd()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _dump_atomic(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


class ConversationWriter:
    """单题对话 writer：逐条 append，全量不裁剪。单题单线程，无锁。"""

    def __init__(self, path: str, code: str):
        self.path = path
        self.code = code
        self._seq = 0
        # 计数既有条目（同题重启 append 时 seq 继续）
        try:
            with open(path, encoding="utf-8") as f:
                self._seq = sum(1 for _ in f)
        except OSError:
            pass
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def write(self, kind: str, **fields: Any) -> None:
        """kind: user | assistant | tool"""
        self._seq += 1
        rec = {"ts": _now(), "seq": self._seq, "code": self.code, "kind": kind}
        rec.update(fields)  # 全量字段，调用方保证不裁剪
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


class RunRecords:
    """当前 bench 的记录门面。"""

    def __init__(self, base_dir: str | None = None, mode: str = "", model: str = ""):
        self.dir = base_dir or RECORDS_DIR
        os.makedirs(self.dir, exist_ok=True)
        self.meta_path = os.path.join(self.dir, "meta.json")
        self.events_path = os.path.join(self.dir, "events.jsonl")
        self.tasks_path = os.path.join(self.dir, "tasks.jsonl")
        self._lock = threading.Lock()

        # meta：首建或补齐缺省字段；历史 runs[] 保留（同 token 重启 append）
        meta: dict = {}
        if os.path.isfile(self.meta_path):
            try:
                with open(self.meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
            except (OSError, json.JSONDecodeError):
                meta = {}
        meta.setdefault("bench_token_hint", "")   # 只存后4位，明文 token 不落盘
        meta.setdefault("mode", mode or "")
        meta.setdefault("model", model or "")
        meta.setdefault("runs", [])
        meta["last_opened_at"] = _now()
        _dump_atomic(self.meta_path, meta)
        self._meta = meta

    # ---- run 级事件 ----
    def event(self, etype: str, data: dict | None = None) -> None:
        rec = {"ts": _now(), "type": etype, **(data or {})}
        with self._lock:
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    # ---- 题目生命周期（append-only）----
    def task_event(self, code: str, phase: str, **fields: Any) -> None:
        rec = {"ts": _now(), "phase": phase, "code": code, **fields}
        with self._lock:
            with open(self.tasks_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    # ---- 运行起止（meta.runs[]）----
    def run_start(self, **fields: Any) -> None:
        with self._lock:
            self._meta["runs"].append({"started_at": _now(), **fields})
            _dump_atomic(self.meta_path, self._meta)

    def run_finish(self, **fields: Any) -> None:
        with self._lock:
            if self._meta["runs"]:
                self._meta["runs"][-1].update({"finished_at": _now(), **fields})
            _dump_atomic(self.meta_path, self._meta)

    # ---- 对话 ----
    def conversation(self, code: str) -> ConversationWriter:
        path = os.path.join(self.dir, "tasks", code, "conversation.jsonl")
        return ConversationWriter(path, code)


REC: "RunRecords | None" = None  # agent 启动时初始化


class _Noop:
    """REC 未初始化时的空实现（smoke/离线场景）。"""

    def event(self, *a, **k): pass
    def task_event(self, *a, **k): pass
    def run_start(self, *a, **k): pass
    def run_finish(self, *a, **k): pass

    def conversation(self, code):
        raise RuntimeError("records not initialized")


_NOOP = _Noop()


def rec() -> "RunRecords | _Noop":
    return REC if REC is not None else _NOOP


def init_records(mode: str = "", model: str = "") -> RunRecords:
    global REC
    REC = RunRecords(mode=mode, model=model)
    return REC
