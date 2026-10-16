"""攻击图确定性状态机（harness-v2-openhands.md §3）。

并发/熔断/预算的唯一切实点：EXPLORING 占一个并发槽，EXPLORED /
DEAD_END 释放。单节点 NODE_TIMEOUT_S 到点由调用方经 expired() +
timeout_node() 熔断（本模块不持有事件循环，纯同步可测）。
非法转移 raise（fail-closed），绝不静默吞掉 planner 的越权决策。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from enum import Enum


class NodeState(Enum):
    UNEXPLORED = "UNEXPLORED"
    EXPLORING = "EXPLORING"
    EXPLORED = "EXPLORED"
    DEAD_END = "DEAD_END"


UNEXPLORED = NodeState.UNEXPLORED
EXPLORING = NodeState.EXPLORING
EXPLORED = NodeState.EXPLORED
DEAD_END = NodeState.DEAD_END


class SlotFullError(Exception):
    pass


class IllegalTransition(Exception):
    pass


class GraphController:
    def __init__(self, *, max_parallel: int = 3, node_timeout_s: float = 900.0) -> None:
        self.max_parallel = max_parallel
        self.node_timeout_s = node_timeout_s
        self._states: dict[str, NodeState] = {}
        self._started: dict[str, float] = {}
        self._parent: dict[str, str | None] = {}

    # -- 结构 ------------------------------------------------------------
    def add_node(self, key: str, *, parent: str | None = None) -> None:
        if key in self._states:
            return  # 幂等：planner 重复委派同一节点视为 no-op
        if parent is not None and parent not in self._states:
            raise IllegalTransition(f"unknown parent {parent!r}")
        self._states[key] = UNEXPLORED
        self._parent[key] = parent

    def _require(self, key: str) -> NodeState:
        state = self._states.get(key)
        if state is None:
            raise IllegalTransition(f"unknown node {key!r}")
        return state

    # -- 迁移 ------------------------------------------------------------
    def begin(self, key: str) -> None:
        state = self._require(key)
        if state is not UNEXPLORED:
            raise IllegalTransition(f"{key}: {state.value} -> EXPLORING 不合法")
        if self.free_slots <= 0:
            raise SlotFullError("no free concurrency slot")
        self._states[key] = EXPLORING
        self._started[key] = time.monotonic()

    def finish(self, key: str, *, explored: bool) -> None:
        self._require(key)
        if self._states[key] is not EXPLORING:
            raise IllegalTransition(f"{key}: finish 只允许 EXPLORING")
        self._states[key] = EXPLORED if explored else DEAD_END
        self._started.pop(key, None)

    def timeout_node(self, key: str) -> None:
        self._require(key)
        if self._states[key] is not EXPLORING:
            return  # 幂等
        self._states[key] = DEAD_END
        self._started.pop(key, None)

    # -- 查询 ------------------------------------------------------------
    def expired(self, *, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        return [k for k, t in self._started.items() if now - t >= self.node_timeout_s]

    def unexplored(self) -> list[str]:
        return [k for k, s in self._states.items() if s is UNEXPLORED]

    @property
    def free_slots(self) -> int:
        exploring = sum(1 for s in self._states.values() if s is EXPLORING)
        return self.max_parallel - exploring

    def snapshot(self) -> dict[str, dict]:
        return {
            k: {"state": s, "started_at": self._started.get(k), "parent": self._parent[k]}
            for k, s in self._states.items()
        }

    def __iter__(self) -> Iterator[str]:
        return iter(self._states)
