"""State Journal —— append-only JSONL（architecture.md §3 State Journal）。

职责：
  - 记录所有状态迁移与平台交互，崩溃后配合 GET /challenges 对账恢复（§3.2）；
  - token 不落盘（Journal 文件可能被审计/公开）。

**hint 事件与 fsync 已随 hint 接口整体移除**（§4.4）：平台状态可对账后，本地
不再持有任何「读不回来」的状态，也就没有必须同步落盘的事件。
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 - 进程生命周期持有

    def append(self, event: str, **fields: object) -> None:
        self._write(event, fields)

    def _write(self, event: str, fields: dict) -> None:
        record = {"ts": round(time.time(), 3), "event": event, **fields}
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def score_summary(rows: list[dict]) -> dict:
    """计分报告：awarded / cumulative_score（runbook.md §2.5 观察点）。"""
    awarded = sum(
        row.get("total_score", 0)
        * row.get("correct_flag_count", 0)
        // max(1, row.get("flag_count", 1))
        for row in rows
    )
    return {
        "awarded": awarded,
        "cumulative_score": awarded,
        "completed": sum(1 for row in rows if row.get("is_completed")),
        "total_flags": sum(row.get("correct_flag_count", 0) for row in rows),
    }
