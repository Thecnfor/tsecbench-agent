"""v2 全链路 e2e：脚本 planner → 图探索 → FakeRunner 产 flag → 验证闸门确认。

不依赖真 LLM、不触网：runner 直接返回成功；flag 由测试预置在 workspace
`out/flags/`（与生产 solver 产物路径一致），验证闸门复用 harvester 正则。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator.v2_bridge import Delegate, Finish
from orchestrator.v2_controller import V2Controller
from orchestrator.v2_verify import verify_claim


class TestV2EndToEnd:
    def test_full_loop_with_verify_gate(self, tmp_path: Path):
        ws = tmp_path / "ws-node-probe"
        ws.mkdir(parents=True)
        (ws / "out" / "flags").mkdir(parents=True)
        (ws / "out" / "flags" / "1.txt").write_text("flag{e2e_dead_beef}", encoding="utf-8")

        class FakeRunner:
            async def __call__(self, *, key, directive, **kw):
                return ("ok", 0, True)

        class Scripted:
            def __init__(self):
                self.n = 0

            def next_action(self, snapshot):
                self.n += 1
                if self.n == 1:
                    return Delegate(key="recon", directive="r")
                if self.n == 2:
                    return Delegate(key="probe", directive="p", parent="recon")
                return Finish("done")

        ctl = V2Controller(runner=FakeRunner(), max_parallel=3, node_timeout_s=900)
        asyncio.run(ctl.run_for_test(planner=Scripted()))
        states = {k: v["state"].value for k, v in ctl.controller.snapshot().items()}
        assert states == {"recon": "EXPLORED", "probe": "EXPLORED"}

        report = verify_claim(ws, runner=lambda: True)
        assert report.passed and report.confirmed == ["flag{e2e_dead_beef}"]
