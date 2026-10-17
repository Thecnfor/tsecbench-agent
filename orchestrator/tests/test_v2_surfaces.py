"""SurfacePlanner：recon 后两个攻击面并行，再合并 prior 进入 exploit。"""

from __future__ import annotations

import asyncio

from orchestrator.v2_controller import SurfacePlanner, V2Controller
from orchestrator.v2_state import EXPLORED


class ConcurrencyRunner:
    def __init__(self) -> None:
        self.ran: list[str] = []
        self.active = 0
        self.max_active = 0

    async def __call__(self, *, key, directive, **kw):
        self.ran.append(key)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.05)
        finally:
            self.active -= 1
        return ("ok", 0, True)


def test_surfaces_run_in_parallel_then_exploit():
    runner = ConcurrencyRunner()
    ctl = V2Controller(runner=runner, max_parallel=2, node_timeout_s=5)
    asyncio.run(ctl.run_for_test(planner=SurfacePlanner()))
    assert runner.ran == ["recon", "probe-web", "probe-creds", "exploit"]
    assert runner.max_active == 2
    states = {key: value["state"] for key, value in ctl.controller.snapshot().items()}
    assert states == {
        "recon": EXPLORED,
        "probe-web": EXPLORED,
        "probe-creds": EXPLORED,
        "exploit": EXPLORED,
    }
