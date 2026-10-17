"""异常路径兜底收割：pipeline 报错前已产出的候选必须被提交。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from orchestrator.journal import Journal
from orchestrator.main import _salvage_candidates
from orchestrator.registry import ChallengeRecord


class FakeClient:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    async def submit(self, unique_code: str, flag: str) -> dict:
        self.submitted.append(flag)
        return {"matched_flag_index": 0, "total_flag_count": 1, "correct_flag_count": 1}


def test_salvage_submits_existing_candidates(tmp_path):
    ws = tmp_path / "ws"
    (ws / "out" / "flags").mkdir(parents=True)
    (ws / "out" / "flags" / "000.txt").write_text("FLAG{salvage_ok}\n", encoding="utf-8")
    rec = ChallengeRecord(unique_code="s1", flag_count=1)
    client = FakeClient()
    journal = Journal(tmp_path / "journal.jsonl")
    ctx = SimpleNamespace(client=client, journal=journal)
    matched = asyncio.run(_salvage_candidates(ctx, rec, ws, "s1"))
    journal.close()
    assert matched == 1
    assert client.submitted == ["FLAG{salvage_ok}"]
    assert "FLAG{salvage_ok}" in rec.submitted
