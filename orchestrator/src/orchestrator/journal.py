"""State Journal —— append-only JSONL（architecture.md §3 State Journal）。

职责：
  - 记录所有状态迁移与平台交互，崩溃后配合 GET /challenges 对账恢复（§3.2）；
  - **hint 记录必须同步 fsync**（§4.4）：「是否已取过 hint」平台读不回来，
    重复取 = 重复扣分，这是崩溃恢复路径上唯一不可对账的状态；
  - token 不落盘（Journal 文件可能被审计/公开）。
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
        self._write(event, fields, fsync=False)

    def append_hint(self, unique_code: str, **fields: object) -> None:
        """hint 事件：同步落盘 + fsync，崩溃恢复不重复扣分（§4.4）。"""
        self._write("hint", {"unique_code": unique_code, **fields}, fsync=True)

    def _write(self, event: str, fields: dict, *, fsync: bool) -> None:
        record = {"ts": round(time.time(), 3), "event": event, **fields}
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        if fsync:
            self._fh.flush()
            import os

            os.fsync(self._fh.fileno())
        else:
            self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    # ---- 恢复 ---------------------------------------------------------------
    def replay(self) -> list[dict]:
        """重放 Journal，恢复本地不可对账状态（hints_taken 等）。"""
        state: dict[str, dict] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # 半行（崩溃时被截断）安全跳过
            event = record.get("event")
            if event == "hint":
                code = record.get("unique_code", "")
                state.setdefault(code, {"hints_taken": 0})
                state[code]["hints_taken"] += 1
            elif event == "submitted":
                code = record.get("unique_code", "")
                entry = state.setdefault(code, {"hints_taken": 0})
                entry.setdefault("submitted", []).append(record.get("flag"))
        return state


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
