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
  绝不截断。因此 conversation.jsonl 同时是「分段解题」恢复上下文的唯一可信来源
  （rebuild_messages 由它重建消息序列）。
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

    def write(self, kind: str, *, started_at: float | None = None,
              ended_at: float | None = None, **fields: Any) -> None:
        """kind: system | user | assistant | tool | note | error

        时间戳（为甘特图准备）：
        - started_at / ended_at：epoch 秒（毫秒精度），该消息/动作的开始与结束；
          输入类（system/user/note）两者同刻；assistant = LLM 调用起止；
          tool = 工具执行起止。未传则默认写入时刻。
        - duration_ms：自动计算（ended - started）。
        - ts：原可读本地时间（兼容旧消费方）。
        """
        self._seq += 1
        now = time.time()
        started = started_at if started_at is not None else now
        ended = ended_at if ended_at is not None else started
        if ended < started:
            ended = started
        rec = {
            "ts": _now(), "seq": self._seq, "code": self.code, "kind": kind,
            "started_at": round(started, 3),
            "ended_at": round(ended, 3),
            "duration_ms": int(round((ended - started) * 1000)),
        }
        rec.update(fields)  # 全量字段，调用方保证不裁剪
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def rebuild_messages(path: str) -> list:
    """从 conversation.jsonl 重建可发送给模型的消息序列（分段解题：暂停→恢复用）。

    只重建会话必需字段：system/user 的 content、assistant 的 content+tool_calls、
    tool 的 result（按 tool_call_id 与 assistant 配对）；thinking/usage 等仅记录字段
    不回传（本来也不发给模型）。尾部裁掉「assistant 已发 tool_calls 但结果不齐」的
    半截对话——OpenAI 协议要求每个 tool_call 后紧跟同 id 的 tool 结果，否则 400。
    返回 [] 表示没有可用上下文（调用方退回全新上下文）。
    """
    msgs: list = []
    pending: set = set()  # 已发出、尚未收到结果的 tool_call id
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    # 只从「最后一次 system」开始重建：真失败后重启会重新写 system+任务卡（新上下文），
    # 更早的段落属于已废弃上下文，不该混进续跑消息里。
    start_at = 0
    for i, line in enumerate(lines):
        try:
            if json.loads(line).get("kind") == "system":
                start_at = i
        except json.JSONDecodeError:
            continue
    for line in lines[start_at:]:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = r.get("kind")
        if kind in ("system", "user"):
            content = r.get("content")
            if isinstance(content, str) and content:
                msgs.append({"role": kind, "content": content})
        elif kind == "assistant":
            tool_calls = r.get("tool_calls") or []
            content = r.get("output")
            if not tool_calls and not content:
                continue
            # 与 solve_task 生成的 assistant 消息结构保持一致（无文本则不带 content 键）
            m: dict = {"role": "assistant"}
            if content:
                m["content"] = content
            if tool_calls:
                m["tool_calls"] = [
                    {"id": t.get("id"), "type": "function",
                     "function": {"name": t.get("name"),
                                  "arguments": t.get("arguments") or "{}"}}
                    for t in tool_calls
                ]
                pending.update(t.get("id") for t in tool_calls)
            msgs.append(m)
        elif kind == "tool":
            tid = r.get("tool_call_id")
            if tid not in pending:
                continue  # 孤儿结果（对应 assistant 缺失或已被裁掉）
            pending.discard(tid)
            result = r.get("result")
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False, default=str)
            msgs.append({"role": "tool", "tool_call_id": tid, "content": result})

    # 尾部一致性：最后一个带 tool_calls 的 assistant，其调用必须全部有结果
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i]["role"] == "assistant" and msgs[i].get("tool_calls"):
            ids = {t.get("id") for t in msgs[i]["tool_calls"]}
            got = {m.get("tool_call_id") for m in msgs[i + 1:] if m.get("role") == "tool"}
            if not ids <= got:
                del msgs[i:]
            break
    if not msgs or msgs[0]["role"] != "system":
        return []
    return msgs


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

    def load_messages(self, code: str) -> list:
        """从对话记录重建消息序列（暂停→恢复续跑用），见 rebuild_messages。"""
        path = os.path.join(self.dir, "tasks", code, "conversation.jsonl")
        return rebuild_messages(path)


REC: "RunRecords | None" = None  # agent 启动时初始化


class _Noop:
    """REC 未初始化时的空实现（smoke/离线场景）。"""

    def event(self, *a, **k): pass
    def task_event(self, *a, **k): pass
    def run_start(self, *a, **k): pass
    def run_finish(self, *a, **k): pass

    def conversation(self, code):
        raise RuntimeError("records not initialized")

    def load_messages(self, code):
        return []


_NOOP = _Noop()


def rec() -> "RunRecords | _Noop":
    return REC if REC is not None else _NOOP


def init_records(mode: str = "", model: str = "") -> RunRecords:
    global REC
    REC = RunRecords(mode=mode, model=model)
    return REC
