"""Staircase 节拍早收割单测（harness-v2.md §2）。

假 solver 先落 flag 文件再挂住，验证：片内按节拍扫出候选并同步提交
（early_submit 事件 + rec.submitted 去重 + completed 信号带出），片尾
outcome 正常返回、不重复计。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from orchestrator.journal import Journal
from orchestrator.registry import ChallengeRecord
from orchestrator.streams import run_solver_with_stream

PY = sys.executable

FAKE = r"""
import json, os, sys, time
from pathlib import Path
ws = Path(os.environ["SOLVER_TASK"]).parent
out = ws / "out" / "flags"
out.mkdir(parents=True, exist_ok=True)
(out / "000.txt").write_text("FLAG{early}\n", encoding="utf-8")
time.sleep(float(os.environ.get("SOLVER_FAKE_WAIT", "1.0")))
(ws / "out" / "result.json").write_text(json.dumps({"status": "solved"}))
"""


class FakeClient:
    """submit 全部命中为正确（mock 语义等价，单测不需要真 server）。"""

    def __init__(self) -> None:
        self.submitted: list[str] = []

    async def submit(self, unique_code: str, flag: str) -> dict:
        self.submitted.append(flag)
        return {"matched_flag_index": 0, "correct_flag_count": 1, "total_flag_count": 1}


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspaces" / "s1_attempt1"
    (ws / "out").mkdir(parents=True)
    (ws / "task.json").write_text(json.dumps({"unique_code": "s1"}), encoding="utf-8")
    return ws


def test_early_harvest_submits_before_solver_exits(tmp_path):
    solver = tmp_path / "fake_stream_solver.py"
    solver.write_text(FAKE, encoding="utf-8")
    ws = _workspace(tmp_path)
    rec = ChallengeRecord(unique_code="s1", flag_count=1, total_score=100)
    journal = Journal(tmp_path / "journal.jsonl")
    client = FakeClient()

    outcome, early_new, early_completed = asyncio.run(
        run_solver_with_stream(
            solver_cmd=[PY, str(solver)],
            ws=ws,
            deadline_epoch=time.time() + 30,
            slice_s=10.0,
            environ={"SOLVER_FAKE_WAIT": "1.0", "PATH": str(Path(PY).parent)},
            client=client,
            journal=journal,
            unique_code="s1",
            rec=rec,
            interval_s=0.2,
        )
    )
    journal.close()

    assert early_new == 1
    assert early_completed is True
    assert client.submitted == ["FLAG{early}"]
    assert "FLAG{early}" in rec.submitted
    assert outcome.exit_code == 0 and not outcome.timed_out
    assert outcome.duration_s < 0.9  # early stop must not wait for the fake 1.0s sleep
    events = [json.loads(ln) for ln in (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert any(e["event"] == "early_submit" and e["new_matches"] == 1 for e in events)
    assert any(e["event"] == "early_stop" for e in events)


def test_no_new_candidates_no_early_submit(tmp_path):
    solver = tmp_path / "fake_quiet_solver.py"
    quiet = FAKE.replace(
        '(out / "000.txt").write_text("FLAG{early}\\n", encoding="utf-8")', "flags = 0"
    )
    solver.write_text(quiet, encoding="utf-8")
    ws = _workspace(tmp_path)
    rec = ChallengeRecord(unique_code="s2", flag_count=1)
    journal = Journal(tmp_path / "journal2.jsonl")

    _outcome, early_new, early_completed = asyncio.run(
        run_solver_with_stream(
            solver_cmd=[PY, str(solver)],
            ws=ws,
            deadline_epoch=time.time() + 30,
            slice_s=10.0,
            environ={"SOLVER_FAKE_WAIT": "0.5", "PATH": str(Path(PY).parent)},
            client=FakeClient(),
            journal=journal,
            unique_code="s2",
            rec=rec,
            interval_s=0.2,
        )
    )
    journal.close()
    assert early_new == 0 and early_completed is False
