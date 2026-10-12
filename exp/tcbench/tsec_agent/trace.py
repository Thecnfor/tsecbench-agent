"""运行追踪日志：模型思考/输出/工具调用与结果/平台生命周期与 flag 变化。

两个输出（同一目录）：
- trace.jsonl — 结构化 JSONL（每行一个事件），便于机器分析
- trace.log   — 人类可读日志

目录选择：$TRACE_DIR > /work（容器挂载卷）> 当前目录。
"""

from __future__ import annotations

import json
import os
import threading
import time

_MAX_FIELD = 20000  # 单字段截断上限（字符）


def _clip(s, limit: int = _MAX_FIELD) -> str:
    if s is None:
        return None
    if not isinstance(s, str):
        s = json.dumps(s, ensure_ascii=False)
    if len(s) <= limit:
        return s
    return s[: limit // 2] + f"\n...[truncated {len(s) - limit} chars]...\n" + s[-(limit // 2):]


class TraceLogger:
    def __init__(self):
        self._lock = threading.Lock()
        d = os.environ.get("TRACE_DIR") or ("/work" if os.access("/work", os.W_OK) else os.getcwd())
        try:
            os.makedirs(d, exist_ok=True)
            self._dir = d
        except OSError:
            self._dir = os.getcwd()
        self.jsonl_path = os.path.join(self._dir, "trace.jsonl")
        self.log_path = os.path.join(self._dir, "trace.log")
        self._seq = 0

    def event(self, etype: str, data: dict, readable: str = None) -> None:
        with self._lock:
            self._seq += 1
            rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "seq": self._seq, "type": etype}
            rec.update(data)
            try:
                with open(self.jsonl_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
                line = readable or f"{etype}: {json.dumps(data, ensure_ascii=False, default=str)[:400]}"
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(f"[{rec['ts']}] {line}\n")
            except OSError:
                pass

    # ---- 模型 ----
    def assistant_turn(self, turn: int, reasoning: str | None, content: str | None,
                       tool_calls: list | None) -> None:
        data = {"turn": turn, "reasoning": _clip(reasoning), "content": _clip(content),
                "tool_calls": tool_calls or []}
        parts = [f"TURN {turn}"]
        if content:
            parts.append(f"output: {_clip(content, 600)}")
        if tool_calls:
            for tc in tool_calls:
                parts.append(f"call {tc['name']}({_clip(json.dumps(tc['arguments'], ensure_ascii=False), 400)})")
        if reasoning:
            parts.append(f"thinking: {_clip(reasoning, 800)}")
        self.event("assistant", data, " | ".join(parts))

    def tool_result(self, turn: int, name: str, arguments: dict, result: str) -> None:
        data = {"turn": turn, "tool": name, "arguments": arguments, "result": _clip(result)}
        self.event("tool_result", data,
                   f"TOOL {name} -> {_clip(result, 400)}")

    # ---- 平台生命周期 ----
    def challenge_change(self, code: str, field: str, old, new, summary: str = "") -> None:
        self.event("challenge_change",
                   {"code": code, "field": field, "old": old, "new": new},
                   f"CHALLENGE {code} {field}: {old} -> {new} {summary}")

    def run_lifecycle(self, phase: str, detail: dict | None = None) -> None:
        self.event("run_lifecycle", {"phase": phase, **(detail or {})},
                   f"RUN {phase} {json.dumps(detail or {}, ensure_ascii=False)[:300]}")


TRACE = TraceLogger()
