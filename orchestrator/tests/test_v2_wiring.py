"""HARNESS_V2 节点 runner：task.json 继承、flag 回传、prior 累积。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from orchestrator.registry import ChallengeRecord
from orchestrator.supervisor import prepare_workspace
from orchestrator.v2_controller import make_stream_runner


class FakeClient:
    async def submit(self, code, flag):
        return {"matched_flag_index": 0, "total_flag_count": 1, "correct_flag_count": 1}


class FakeJournal:
    def append(self, *args, **kwargs):
        pass


def test_node_runner_inherits_task_and_returns_flags(tmp_path):
    rec = ChallengeRecord(unique_code="t1", container_addr=["10.0.0.1:80"])
    ws = prepare_workspace(tmp_path, rec, attempt=1)
    solver = tmp_path / "fake_solver.py"
    solver.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "ws = Path(os.environ['SOLVER_TASK']).parent\n"
        "(ws / 'out' / 'flags').mkdir(parents=True, exist_ok=True)\n"
        "(ws / 'out' / 'flags' / '000.txt').write_text('FLAG{node_ok}\\n')\n",
        encoding="utf-8",
    )
    runner = make_stream_runner(
        solver_cmd=[sys.executable, str(solver)],
        base_environ={"PATH": os.environ["PATH"]},
        slice_s=10,
        ws=ws,
        client=FakeClient(),
        journal=FakeJournal(),
        rec=rec,
        deadline_epoch=time.time() + 60,
    )
    _text, code, ok = asyncio.run(runner(key="recon", directive="map it"))
    assert code == 0 and ok
    node_task = json.loads((ws / "node-recon" / "task.json").read_text(encoding="utf-8"))
    assert "# NODE FOCUS" in node_task["description"]
    assert (ws / "out" / "flags" / "recon_000.txt").read_text(
        encoding="utf-8"
    ).strip() == "FLAG{node_ok}"
