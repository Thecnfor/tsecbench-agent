"""Staircase 流式泵 —— pi stdout 逐行实时消费（harness-v2.md §2）。

原 dual_main 用 `proc.communicate()` 整批等 pi 退出后才处理 stdout：
下游（flag 收割 / session 事件流 / prior 合成）必须等上游「全部完成」。
本模块把它变成 Staircase Streaming：逐行到哪儿、处理到哪儿 ——
候选实时落 out/flags/、事件实时进 session、prior 按节拍增量预写。

约束：线程 + 队列，killpg 后读端 EOF 自然收束；收集上限 HARD_CAP_S
兜底，任何异常都不让收集循环挂死（§2.3）。
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time

_SENTINEL = object()
HARD_CAP_S = 20.0  # EOF 迟迟不来时的最长继续排水时长（killpg 走 ProcessLookup 已必 EOF）
FLUSH_EVERY = 40  # 每消费 N 行增量预写一次 prior（Staircase 下游节拍，§2.2）


class PipeReader:
    """后台线程把 piped stdout 逐行推入队列；主线程按需逐行（或带超时）取出。"""

    def __init__(self, stream) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._eof = threading.Event()
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream) -> None:
        try:
            for raw in iter(stream.readline, ""):
                if not raw:
                    break
                self._queue.put(raw)
        except (OSError, ValueError):
            pass  # 管道被 kill/关闭：自然 EOF
        finally:
            self._queue.put(_SENTINEL)

    @property
    def eof(self) -> bool:
        return self._eof.is_set()

    def next_line(self, timeout: float) -> str | None:
        """取一行；None = 超时未到，"" = EOF。"""
        try:
            item = self._queue.get(timeout=max(0.01, timeout))
        except queue.Empty:
            return None
        if item is _SENTINEL:
            self._eof.set()
            return ""
        return item

    def join(self, timeout: float) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)


def stream_pi(proc, on_line, *, budget_s: float, on_expire=None) -> tuple[str, bool]:
    """实时消费 proc.stdout 直到进程退出或超时。

    返回 (整段累计文本, 是否跨过 budget 被逐出)。行处理（事件落盘 / 候选
    实时报告 / 增量 prior 预写）全部委托 on_line(line)；超时逻辑与原
    `communicate(timeout)` 同款：到点杀整进程组，随后排水剩余行。永不挂死、
    永不抛出。
    """
    started = time.monotonic()
    reader = PipeReader(proc.stdout)
    chunks: list[str] = []
    expired = False
    while not reader.eof:
        remaining = budget_s - (time.monotonic() - started)
        if remaining <= 0 and expired is False and on_expire is not None:
            expired = True
            on_expire()  # 到点杀整进程组（§2.3：killpg，不留孤儿）
        wait = 0.5 if remaining > 0.5 else max(0.05, min(0.5, remaining))
        line = reader.next_line(timeout=wait)
        if line is None:
            # EOF 未到也未见进程退出：超过 HARD_CAP 继续死等会挂死，强制收束
            if time.monotonic() - started > budget_s + HARD_CAP_S:
                break
            continue
        if line == "":
            continue  # EOF 标记下一轮退出
        chunks.append(line)
        with contextlib.suppress(Exception):
            on_line(line)  # 单行处理失败绝不中断流路（下游容错原则）
        if expired and time.monotonic() - started > budget_s + HARD_CAP_S:
            break
    reader.join(timeout=2.0)
    return "".join(chunks), expired
