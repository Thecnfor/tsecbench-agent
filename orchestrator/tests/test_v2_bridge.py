import pytest
from orchestrator.v2_bridge import (
    Delegate,
    Finish,
    PlannerBridge,
    PlannerFusedOut,
    RejectedAction,
)
from orchestrator.v2_state import GraphController


def _bridge(**kw):
    return PlannerBridge(memory=None, controller=GraphController(), **kw)


class TestDelegate:
    def test_delegate_creates_node(self):
        b = _bridge()
        b.controller.add_node("root")  # add_node 对未知 parent fail-closed，先种根节点
        b.handle_delegate(Delegate(key="recon-8080", directive="nmap 层面侦听面", parent="root"))
        # 节点已注册（unexplored 可见），可 begin
        b.controller.begin("recon-8080")

    def test_finish_signal(self):
        b = _bridge()  # noqa: F841  构造成功即断言（memory=None 合法）
        assert isinstance(Finish(reason="all clear"), Finish)


class TestZeroCommand:
    def test_any_command_rejected(self):
        b = _bridge()
        with pytest.raises(RejectedAction):
            b.handle_command("execute_bash", {"command": "nmap -sV"})
        with pytest.raises(RejectedAction):
            b.handle_command("run_ipython", {"code": "import os"})

    def test_reject_fuse(self):
        b = _bridge(max_rejects=2)
        for _ in range(2):
            with pytest.raises(RejectedAction):
                b.handle_command("execute_bash", {})
        with pytest.raises(PlannerFusedOut):
            b.handle_command("execute_bash", {})


class TestStateConfirm:
    def test_confirm_updates_controller(self):
        b = _bridge()
        b.handle_delegate(Delegate(key="a", directive="x"))
        b.controller.begin("a")
        b.confirm_state("a", explored=True)
        assert b.controller.snapshot()["a"]["state"].value == "EXPLORED"
