"""OpenHands 桥接层（harness-v2-openhands.md §2）。

planner 的全部输出在这里过闸：Delegate 意图入状态机并落 .tdb；
任何命令执行类 Action 一律 RejectedAction（fail-closed）——planner
不执行具体命令是本层的不变量，超限熔断整个 planner。
"""

from __future__ import annotations

from dataclasses import dataclass

from .memory import AttackMemory
from .v2_state import GraphController, NodeState

_STATE_NAME = {s: s.value for s in NodeState}


@dataclass
class Delegate:
    key: str
    directive: str
    parent: str | None = None


@dataclass
class Finish:
    reason: str = ""


@dataclass
class Wait:
    """planner 暂时不能委派（父阶段还在 EXPLORING）；下一拍重试。"""

    reason: str = ""


class RejectedAction(Exception):
    pass


class PlannerFusedOut(Exception):
    pass


class PlannerBridge:
    def __init__(
        self, *, memory: AttackMemory | None, controller: GraphController, max_rejects: int = 3
    ) -> None:
        self.memory = memory
        self.controller = controller
        self.max_rejects = max_rejects
        self._rejects = 0

    def handle_delegate(self, d: Delegate) -> None:
        self.controller.add_node(d.key, parent=d.parent)
        if self.memory is not None:
            self.memory.record_node(
                d.key, parent=d.parent, state=NodeState.UNEXPLORED.value, directive=d.directive
            )

    def handle_command(self, tool_name: str, args: dict) -> None:
        del args  # 命令内容不落任何存储 —— planner 侧命令零通道
        self._rejects += 1
        if self._rejects > self.max_rejects:
            raise PlannerFusedOut(f"planner 试图执行命令 {self._rejects} 次，熔断")
        raise RejectedAction(
            f"REJECTED: planner 不执行命令（{tool_name}），请委派 AgentDelegateAction"
        )

    def confirm_state(self, key: str, *, explored: bool) -> None:
        self.controller.finish(key, explored=explored)
        if self.memory is not None:
            self.memory.record_node(
                key,
                parent=None,
                state=(NodeState.EXPLORED if explored else NodeState.DEAD_END).value,
            )
