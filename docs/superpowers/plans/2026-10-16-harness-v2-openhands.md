# Harness v2 OpenHands 控制面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 `docs/harness-v2-openhands.md` 契约：确定性攻击图状态机 + OpenHands 桥接（planner 零命令）+ TriviumDB 单写多读 + 独立 flag 验证闸门 + SOP 渐进披露，`HARNESS_V2` 开关旁路，stub planner e2e 全链路。

**Architecture:** v2_controller（Python 确定性状态机）持并发槽与熔断；Planner（stub / OpenHands）只产 Delegate 意图，经 v2_bridge 入队；每节点 fork 一次 dual_main 子进程（复用 `streams.run_solver_with_stream`）；桥接层经 `memory.AttackMemory` 唯一写 .tdb，Pi 只读 prior.json 契约不变。

**Tech Stack:** Python 3.13 / pytest / triviumdb 0.8.8（可选依赖，缺失即退化空操作，沿用 memory.py 既有模式）/ openhands-ai 1.11.0（冒烟阶段才真跑）。

## Global Constraints

- 行为契约：`docs/harness-v2-openhands.md`（§1 不变式全文适用于每个任务）
- `BENCHMARK_TOKEN` 绝不进 solver/planner 环境；镜像与 prompt 不含 flag 字面量与题目特化内容
- 运行期零公网；`CONCURRENCY_LIMIT ≤ 3` 启动即校验（config.py:111-113 现有闸门不动）
- `prior.json` 双图 JSON 通道契约零改动；Node napi 绑定（B15）不在本计划
- ruff: line-length 100, target py313, 规则 E,F,I,UP,B,SIM；提交前 `make lint && make test`（当前基线 188 passed + 1 skipped）
- 测试完全离线：不依赖真 LLM key、真 OpenHands、真平台 API
- 提交信息以 `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>` 结尾

---

### Task 1: HARNESS_V2 特性开关

**Files:**
- Modify: `orchestrator/src/orchestrator/config.py`
- Test: `orchestrator/tests/test_config.py`

**Interfaces:**
- Produces: `config.HARNESS_V2: bool`（env `HARNESS_V2`，默认 `False`）。后续所有 v2 模块以此为总闸。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 orchestrator/tests/test_config.py 末尾
class TestHarnessV2Flag:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("HARNESS_V2", raising=False)
        from orchestrator.config import HARNESS_V2
        assert HARNESS_V2 is False

    def test_on(self, monkeypatch):
        monkeypatch.setenv("HARNESS_V2", "on")
        import importlib
        from orchestrator import config
        importlib.reload(config)
        assert config.HARNESS_V2 is True
```

（注意 config 读取时机：仿照该文件现有 env 常量模式 —— 若现有常量是模块级 `os.environ.get(...)`，同款追加 `HARNESS_V2 = _bool("HARNESS_V2", False)`；`_bool` 不存在则复用 `_int` 同款写法新增。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest orchestrator/tests/test_config.py -k harness_v2 -v`
Expected: FAIL / AttributeError

- [ ] **Step 3: 实现**

`orchestrator/src/orchestrator/config.py` 在 `_int` 定义后加：

```python
def _bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "on", "true", "yes")


HARNESS_V2 = _bool("HARNESS_V2", False)
```

- [ ] **Step 4: 跑测试通过 + 全量回归**

Run: `uv run pytest orchestrator/tests/test_config.py -v && make lint`
Expected: PASS，lint 绿

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/config.py orchestrator/tests/test_config.py
git commit -m "feat(v2): HARNESS_V2 特性开关（默认 off，旁路零回归）"
```

---

### Task 2: 攻击图状态机 v2_state

**Files:**
- Create: `orchestrator/src/orchestrator/v2_state.py`
- Test: `orchestrator/tests/test_v2_state.py`

**Interfaces:**
- Produces:
  - `NodeState` 枚举：`UNEXPLORED / EXPLORING / EXPLORED / DEAD_END`
  - `class SlotFullError(Exception)`、`class IllegalTransition(Exception)`
  - `class GraphController(max_parallel: int = 3, node_timeout_s: float = 900.0)`
  - 方法：`add_node(key: str, *, parent: str | None = None) -> None`、
    `begin(key: str) -> None`（占并发槽）、
    `finish(key: str, *, explored: bool) -> None`、
    `timeout_node(key: str) -> None`（强制 DEAD_END + 释放槽）、
    `expired(now: float) -> list[str]`、
    `unexplored() -> list[str]`、`free_slots -> int`、
    `snapshot() -> dict`（key → state + started_at，供观测/审计）

- [ ] **Step 1: 写失败测试**

```python
import pytest

from orchestrator.v2_state import (
    DEAD_END, EXPLORED, EXPLORING, UNEXPLORED, NodeState,
    GraphController, IllegalTransition, SlotFullError,
)


class TestTransitions:
    def test_seed_and_explore_path(self):
        gc = GraphController(max_parallel=2, node_timeout_s=900)
        gc.add_node("root")
        assert gc.unexplored() == ["root"]
        gc.begin("root")
        gc.finish("root", explored=True)
        assert gc.free_slots == 2

    def test_dead_end_is_terminal(self):
        gc = GraphController()
        gc.add_node("a"); gc.begin("a"); gc.timeout_node("a")
        with pytest.raises(IllegalTransition):
            gc.begin("a")

    def test_exploring_requires_unexplored(self):
        gc = GraphController()
        with pytest.raises(IllegalTransition):
            gc.begin("ghost")

    def test_double_begin_rejected(self):
        gc = GraphController()
        gc.add_node("a"); gc.begin("a")
        with pytest.raises(IllegalTransition):
            gc.begin("a")


class TestConcurrency:
    def test_slots_exhausted(self):
        gc = GraphController(max_parallel=3)
        for k in ("a", "b", "c"):
            gc.add_node(k); gc.begin(k)
        assert gc.free_slots == 0
        gc.add_node("d")
        with pytest.raises(SlotFullError):
            gc.begin("d")

    def test_finish_releases_slot(self):
        gc = GraphController(max_parallel=1)
        gc.add_node("a"); gc.begin("a")
        gc.add_node("b")
        with pytest.raises(SlotFullError):
            gc.begin("b")
        gc.finish("a", explored=False)   # 非超时失败也释放槽
        gc.begin("b")
        assert gc.free_slots == 0


class TestTimeoutFuse:
    def test_expired_and_force_dead_end(self):
        gc = GraphController(max_parallel=2, node_timeout_s=10)
        gc.add_node("a"); gc.begin("a")
        gc.add_node("b"); gc.begin("b")
        stale = gc.expired(now=_clock() + 11)
        assert sorted(stale) == ["a", "b"]
        for k in stale:
            gc.timeout_node(k)
        assert gc.free_slots == 2
        assert gc.snapshot()["a"]["state"] is DEAD_END


def _clock() -> float:
    import time
    return time.monotonic()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest orchestrator/tests/test_v2_state.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: 实现**

```python
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
        return [
            k
            for k, t in self._started.items()
            if now - t >= self.node_timeout_s
        ]

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
```

（`NodeState` 是 Enum，测试里 `is DEAD_END` 用的是 `gc.snapshot()` 里存的枚举值本身，成立。）

- [ ] **Step 4: 跑测试通过**

Run: `uv run pytest orchestrator/tests/test_v2_state.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/v2_state.py orchestrator/tests/test_v2_state.py
git commit -m "feat(v2): 攻击图确定性状态机（UNEXPLORED→EXPLORING→EXPLORED/DEAD_END + 并发槽 + 熔断）"
```

---

### Task 3: TriviumDB 单写多读（writer 侧固化 + readonly 打开）

**Files:**
- Modify: `orchestrator/src/orchestrator/memory.py`
- Test: `orchestrator/tests/test_memory.py`

**Interfaces:**
- Consumes: 既有 `AttackMemory(path, enabled=..., dim=64)`、`record/mark/recall/unexplored`。
- Produces: `AttackMemory.open_readonly(path: Path) -> AttackMemory`（类方法，只读句柄：写方法 raise，`ReadOnlyError` 语义固化）；`AttackMemory.record_node(key: str, *, parent: str | None, state: str, directive: str = "") -> None`（v2 状态机经桥接层落图的唯一入口）。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 orchestrator/tests/test_memory.py
class TestReadOnly:
    def test_readonly_rejects_write(self, tmp_path):
        from orchestrator.memory import AttackMemory
        writer = AttackMemory(tmp_path / "a.tdb")
        if not writer.available():
            pytest.skip("triviumdb not installed")
        writer.record_node("root", parent=None, state="EXPLORED")
        ro = AttackMemory.open_readonly(tmp_path / "a.tdb")
        with pytest.raises(Exception):
            ro.record_node("child", parent="root", state="UNEXPLORED")
        assert ro.reachable_nodes() >= 1   # 只读侧能读回
        writer.close(); ro.close()

    def test_record_node_graph_edges(self, tmp_path):
        from orchestrator.memory import AttackMemory
        m = AttackMemory(tmp_path / "b.tdb")
        if not m.available():
            pytest.skip("triviumdb not installed")
        m.record_node("root", parent=None, state="EXPLORED")
        m.record_node("n1", parent="root", state="DEAD_END")
        assert m.reachable_nodes() == 2
        m.close()
```

（若 test_memory.py 已有「triviumdb 未装即 skip」的既有 fixture/模式，复用它。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest orchestrator/tests/test_memory.py -k "readonly or record_node" -v`
Expected: AttributeError: open_readonly

- [ ] **Step 3: 实现（memory.py 追加）**

```python
    # ---- v2：单写多读（harness-v2-openhands.md §3）----
    @classmethod
    def open_readonly(cls, path: Path) -> "AttackMemory":
        m = cls(path)
        if m._db is not None:
            m._db.set_readonly(True)   # 若 0.8.8 无此方法，退化为包一层写拒绝（见 record）
        return m

    def record_node(self, key: str, *, parent: str | None, state: str,
                    directive: str = "") -> None:
        """v2 状态机经桥接层落图的唯一入口：节点 + 父边 + 状态文本一次写。"""
        if self._db is None:
            return
        if self._readonly:
            from triviumdb import ReadOnlyError  # noqa: F401  POC 已验证该异常存在
            raise ReadOnlyError("AttackMemory.open_readonly handle is read-only")
        text = f"{state} {directive}".strip() or state
        self._db.insert_with_id(_node_id(key), text,
                                metadata={"key": key, "state": state})
        if parent is not None:
            self._db.link(_node_id(parent), _node_id(key))

    def reachable_nodes(self) -> int:
        if self._db is None:
            return 0
        return self._db.reachable(_node_id(_ROOT_SENTINEL), max_depth=99).__len__() \
            if False else int(self._stats_nodes())
```

实现注意（写代码时以实测为准，三选一取最简可行）：
1. `set_readonly` 若不存在：`__init__` 加 `self._readonly = False` 参数位，`open_readonly` 置 True，`record/record_node` 头部检查 `self._readonly` 抛 `ReadOnlyError`（不依赖底层 API）；
2. `reachable_nodes`：优先用 `neighbors/max_depth` 遍历计数，不行就用 `stats()` 里已有的节点数（`stats()` 已存在，见 memory.py:165）；
3. `_ROOT_SENTINEL` 不引入 —— `reachable_nodes` 直接实现为「从每个已记录 key 出发可达并集计数」或退化为 `stats()` 的节点计数，以 POC 实测 API 为准。

- [ ] **Step 4: 跑测试（triviumdb 已装则全 PASS，未装 skip）+ 全量回归**

Run: `uv run pytest orchestrator/tests/test_memory.py -v && make lint`
Expected: PASS（或 triviumdb 缺失时 skip，不 FAIL）

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/memory.py orchestrator/tests/test_memory.py
git commit -m "feat(v2): TriviumDB 单写多读 —— open_readonly + record_node 状态机落图入口"
```

---

### Task 4: 桥接层 v2_bridge（planner 零命令）

**Files:**
- Create: `orchestrator/src/orchestrator/v2_bridge.py`
- Test: `orchestrator/tests/test_v2_bridge.py`

**Interfaces:**
- Consumes: Task 2 的 `GraphController`、Task 3 的 `AttackMemory.record_node`。
- Produces:
  - `@dataclass Delegate: key: str; directive: str; parent: str | None = None`
  - `@dataclass Finish: reason: str = ""`
  - `class RejectedAction(Exception)`（含 `reason` 属性）
  - `class PlannerBridge(memory: AttackMemory | None, controller: GraphController, *, max_rejects: int = 3)`
  - 方法：`handle_delegate(d: Delegate) -> None`（建节点 + 落 .tdb；node 状态 UNEXPLORED）、
    `handle_command(tool_name: str, args: dict) -> None`（**一律 raise RejectedAction**，计数超 `max_rejects` 后 `PlannerFusedOut`）、
    `class PlannerFusedOut(Exception)`、
    `confirm_state(key: str, *, explored: bool) -> None`（节点迁移 + .tdb 状态更新）

- [ ] **Step 1: 写失败测试**

```python
import pytest

from orchestrator.v2_bridge import (
    Delegate, Finish, PlannerBridge, PlannerFusedOut, RejectedAction,
)
from orchestrator.v2_state import GraphController


def _bridge(**kw):
    return PlannerBridge(memory=None, controller=GraphController(), **kw)


class TestDelegate:
    def test_delegate_creates_node(self):
        b = _bridge()
        b.handle_delegate(Delegate(key="recon-8080", directive="nmap 层面侦听面", parent="root"))
        # 节点已注册（unexplored 可见），可 begin
        b.controller.begin("recon-8080")

    def test_finish_signal(self):
        b = _bridge()
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest orchestrator/tests/test_v2_bridge.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: 实现**

```python
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


class RejectedAction(Exception):
    pass


class PlannerFusedOut(Exception):
    pass


class PlannerBridge:
    def __init__(self, *, memory: AttackMemory | None,
                 controller: GraphController, max_rejects: int = 3) -> None:
        self.memory = memory
        self.controller = controller
        self.max_rejects = max_rejects
        self._rejects = 0

    def handle_delegate(self, d: Delegate) -> None:
        self.controller.add_node(d.key, parent=d.parent)
        if self.memory is not None:
            self.memory.record_node(d.key, parent=d.parent,
                                    state=NodeState.UNEXPLORED.value,
                                    directive=d.directive)

    def handle_command(self, tool_name: str, args: dict) -> None:
        del args  # 命令内容不落任何存储 —— planner 侧命令零通道
        self._rejects += 1
        if self._rejects > self.max_rejects:
            raise PlannerFusedOut(
                f"planner 试图执行命令 {self._rejects} 次，熔断"
            )
        raise RejectedAction(
            f"REJECTED: planner 不执行命令（{tool_name}），请委派 AgentDelegateAction"
        )

    def confirm_state(self, key: str, *, explored: bool) -> None:
        self.controller.finish(key, explored=explored)
        if self.memory is not None:
            self.memory.record_node(key, parent=None,
                                    state=(NodeState.EXPLORED if explored
                                           else NodeState.DEAD_END).value)
```

（`handle_command` 里 `raise` 后的 `PlannerFusedOut` 分支：注意顺序 —— 先计数判熔断再 raise Rejected，如上已正确。）

- [ ] **Step 4: 跑测试通过 + lint**

Run: `uv run pytest orchestrator/tests/test_v2_bridge.py -v && make lint`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/v2_bridge.py orchestrator/tests/test_v2_bridge.py
git commit -m "feat(v2): planner 桥接层 —— 委派入图、命令零通道、超限熔断"
```

---

### Task 5: 独立验证器 v2_verify

**Files:**
- Create: `orchestrator/src/orchestrator/v2_verify.py`
- Test: `orchestrator/tests/test_v2_verify.py`

**Interfaces:**
- Produces:
  - `FLAG_PATTERN: re.Pattern`（与 harvester 同组形态：`flag|FLAG|ctf|CTF|HTB` 前缀 + `{...}`；**实现时 import harvester 既有正则作单一真值源，勿复制**）
  - `@dataclass VerifyReport: claimed: list[str]; confirmed: list[str]; liveness: bool`（`passed` property：confirmed 非空且 liveness）
  - `def verify_claim(ws: Path, *, runner: Callable[[], bool] | None = None) -> VerifyReport` —— 扫 workspace 内 flag 形态文件内容 + shell 往返存活探测（默认 runner 用 `subprocess.run(["sh", "-c", "echo $RANDOM"])` 且 stdout 非空）。

- [ ] **Step 1: 写失败测试**

```python
from pathlib import Path

from orchestrator.v2_verify import VerifyReport, verify_claim


def _ws(tmp_path: Path, flag: str | None) -> Path:
    out = tmp_path / "out" / "flags"
    out.mkdir(parents=True)
    if flag:
        (out / "1.txt").write_text(flag, encoding="utf-8")
    return tmp_path


class TestVerify:
    def test_no_claim(self, tmp_path):
        r = verify_claim(_ws(tmp_path, None), runner=lambda: True)
        assert r.claimed == [] and not r.passed

    def test_real_flag_confirmed(self, tmp_path):
        r = verify_claim(_ws(tmp_path, "flag{cafe_babe_42}"), runner=lambda: True)
        assert r.claimed == ["flag{cafe_babe_42}"]
        assert r.confirmed == r.claimed and r.passed

    def test_liveness_false_fails(self, tmp_path):
        r = verify_claim(_ws(tmp_path, "flag{aabb}"), runner=lambda: False)
        assert not r.liveness and not r.passed
        assert r.claimed  # 声称仍在，但不通过

    def test_shell_roundtrip_default_runner(self, tmp_path):
        # 真实 sh 往返（离线可跑）
        r = verify_claim(_ws(tmp_path, None))
        assert r.liveness is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest orchestrator/tests/test_v2_verify.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: 实现**

```python
"""独立验证闸门（harness-v2-openhands.md §4）：模型不得自宣成功。

声称 ≠ 通过：workspace 里的 flag 形态内容只有同时通过
(a) harvester 同一正则复核 (b) shell 往返存活探测，才允许进 submit。
本模块零 LLM、零网络（runner 除外）。
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .harvester import FLAG_RE  # 单一真值源：与收割同一形态正则（如名不同，对齐后引用）


@dataclass
class VerifyReport:
    claimed: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    liveness: bool = False

    @property
    def passed(self) -> bool:
        return bool(self.confirmed) and self.liveness


def _shell_alive() -> bool:
    try:
        p = subprocess.run(["sh", "-c", "echo $RANDOM"],
                           capture_output=True, text=True, timeout=10)
        return p.returncode == 0 and p.stdout.strip() != ""
    except (OSError, subprocess.TimeoutExpired):
        return False


def verify_claim(ws: Path, *, runner: Callable[[], bool] | None = None) -> VerifyReport:
    alive = (runner or _shell_alive)()
    claimed: list[str] = []
    confirmed: list[str] = []
    flags_dir = ws / "out" / "flags"
    if flags_dir.is_dir():
        for f in sorted(flags_dir.iterdir()):
            if not f.is_file():
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            found = FLAG_RE.findall(text)
            for m in found:
                token = m if isinstance(m, str) else m[0]
                if token not in claimed:
                    claimed.append(token)
                    if alive:
                        confirmed.append(token)
    return VerifyReport(claimed=claimed, confirmed=confirmed, liveness=alive)
```

实现注意：`harvester.py` 里正则的真实名字先 `grep -n "re.compile" orchestrator/src/orchestrator/harvester.py` 确认——若不叫 `FLAG_RE`，import 处对齐为真名；若 harvester 未导出，把它提为模块级常量再引用（最小 diff）。

- [ ] **Step 4: 跑测试通过 + lint**

Run: `uv run pytest orchestrator/tests/test_v2_verify.py -v && make lint`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/v2_verify.py orchestrator/tests/test_v2_verify.py
git commit -m "feat(v2): 独立验证闸门 —— flag 形态复核 + shell 往返存活，声称≠通过"
```

---

### Task 6: SOP 渐进式披露（两级注入）

**Files:**
- Modify: `orchestrator/src/orchestrator/sop.py`
- Test: `orchestrator/tests/test_sop.py`

**Interfaces:**
- Consumes: 既有 `distill/select/mark_used`（sop.py:103/185/175）与脱敏闸门 `_sanitize/_contains_flag`。
- Produces:
  - `def catalog(base: Path, *, top_k: int = 8) -> list[tuple[str, str]]` —— `[(slug, 一行摘要)]`，摘要 = 正文第一行截断 80 字符，经 `_sanitize`；
  - `def load(base: Path, slug: str) -> str | None` —— 全文，读侧过 flag 闸门（`_contains_flag` 命中 → 返回 None，fail-closed，与 §4.2 同位）。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 orchestrator/tests/test_sop.py
class TestProgressiveDisclosure:
    def test_catalog_and_load(self, tmp_path):
        from orchestrator import sop
        base = tmp_path / "sops"
        base.mkdir()
        # 直接构造正文文件（绕过 distill，聚焦读侧）
        body = base / "web-sqli.md"
        body.write_text("第一步：探测注入点\n第二步：sqlmap 批处理\n", encoding="utf-8")
        idx = base / "index.json"
        import json
        idx.write_text(json.dumps({"entries": [{"slug": "web-sqli", "wins": 1, "uses": 0}]}),
                       encoding="utf-8")
        cat = sop.catalog(base)
        assert cat and cat[0][0] == "web-sqli"
        full = sop.load(base, "web-sqli")
        assert "sqlmap" in full

    def test_load_rejects_flag_body(self, tmp_path):
        from orchestrator import sop
        base = tmp_path / "sops"; base.mkdir()
        (base / "dirty.md").write_text("flag{leak}\n", encoding="utf-8")
        (base / "index.json").write_text(
            '{"entries": [{"slug": "dirty", "wins": 1, "uses": 0}]}', encoding="utf-8")
        assert sop.load(base, "dirty") is None

    def test_load_missing_slug_none(self, tmp_path):
        from orchestrator import sop
        assert sop.load(tmp_path, "ghost") is None
```

（index.json 结构以 `_read_index/_write_index` 真实格式为准 —— 写实现前先读 sop.py:77-91 对齐字段名；测试 fixture 相应调整。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest orchestrator/tests/test_sop.py -k progressive -v`
Expected: AttributeError: catalog

- [ ] **Step 3: 实现（sop.py 追加）**

```python
_SUMMARY_CHARS = 80


def catalog(base: Path, *, top_k: int = 8) -> list[tuple[str, str]]:
    """System Prompt 只给目录：slug + 一行脱敏摘要（harness-v2-openhands.md §5）。"""
    index = _read_index(base)
    out: list[tuple[str, str]] = []
    for slug in index:
        text = _read_body(base, slug)  # 若无 _read_body 助手，就地实现：正文文件读文本
        if not text or _contains_flag(text):
            continue
        summary = _sanitize(text.strip().splitlines()[0] if text.strip() else "")[:_SUMMARY_CHARS]
        out.append((slug, summary))
    return out[:top_k]


def load(base: Path, slug: str) -> str | None:
    """按需注入全文；flag 形态命中即拒绝（fail-closed，同 §4.2 检索闸）。"""
    text = 正文读取(base, slug)  # 同上对齐既有读取方式
    if not text or _contains_flag(text):
        return None
    return _sanitize(text)
```

- [ ] **Step 4: 跑测试通过 + 全量回归**

Run: `uv run pytest orchestrator/tests/test_sop.py -v && make lint`
Expected: PASS（含既有 SOP 测试不回归）

- [ ] **Step 5: Commit**

```bash
git add orchestrator/src/orchestrator/sop.py orchestrator/tests/test_sop.py
git commit -m "feat(v2): SOP 渐进披露 —— catalog 目录注入 + load 全文按需 + fail-closed 闸"
```

---

### Task 7: v2_controller 主控 + node 级执行复用

**Files:**
- Create: `orchestrator/src/orchestrator/v2_controller.py`
- Modify: `solver/dual_main.py`（新增 `SOLVER_NODE_DIRECTIVE` env 注入位，追加到 APPEND_SYSTEM 尾部；≤10 行 diff）
- Test: `orchestrator/tests/test_v2_controller.py` + `orchestrator/tests/test_dual_solver_contract.py`（回归）

**Interfaces:**
- Consumes: Task 2-5 全部；既有 `streams.run_solver_with_stream`（签名见 streams.py:24）、`harvester.harvest/submit_candidates`、`config.HARNESS_V2`、`sop.catalog/load`。
- Produces:
  - `class V2Controller`：`async def run_task(*, task, ws, client, journal, rec, deadline_epoch, total_budget_s, planner) -> None`
  - planner 协议：`def next_action(snapshot: dict) -> Delegate | Finish`（同步、确定性；StubPlanner 实现，OpenHandsPlanner 决赛冒烟时另接）
  - dual_main 侧：`SOLVER_NODE_DIRECTIVE` 环境变量存在时，其内容追加进 APPEND_SYSTEM（`REPAIR FOCUS` 段同位注入）。

- [ ] **Step 1: 写失败测试（stub planner 状态机联动，不起子进程）**

```python
import asyncio

import pytest

from orchestrator.v2_bridge import Delegate, Finish
from orchestrator.v2_controller import StubPlanner, V2Controller
from orchestrator.v2_state import DEAD_END, EXPLORED


class ScriptedPlanner:
    """按脚本吐 Delegate，验证 controller 与 bridge 的联动时序。"""

    def __init__(self, script: list[Delegate | Finish]):
        self.script = list(script)
        self.seen_snapshots: list[dict] = []

    def next_action(self, snapshot: dict) -> Delegate | Finish:
        self.seen_snapshots.append(snapshot)
        return self.script.pop(0) if self.script else Finish("script done")


class FakeRunner:
    """替代 run_solver_with_stream 的最小桩：记录委派的 key，秒回成功。"""

    def __init__(self):
        self.ran: list[str] = []

    async def __call__(self, *, key, directive, **kw):
        self.ran.append(key)
        return ("ok", 0, True)


class TestControllerLoop:
    def test_linear_chain(self):
        runner = FakeRunner()
        planner = ScriptedPlanner([
            Delegate(key="recon", directive="d1"),
            Delegate(key="probe", directive="d2", parent="recon"),
            Finish("done"),
        ])
        ctl = V2Controller(runner=runner, max_parallel=3, node_timeout_s=900)
        asyncio.run(ctl.run_for_test(planner=planner))
        assert runner.ran == ["recon", "probe"]

    def test_timeout_forces_dead_end_and_releases_slot(self):
        class TimeoutRunner(FakeRunner):
            async def __call__(self, *, key, directive, **kw):
                await asyncio.sleep(0.05)
                return ("timeout", 1, False)

        planner = ScriptedPlanner([
            Delegate(key="a", directive="x"),
            Delegate(key="b", directive="y"),
        ])
        ctl = V2Controller(runner=TimeoutRunner(), max_parallel=1, node_timeout_s=0.01)
        asyncio.run(ctl.run_for_test(planner=planner))
        states = {k: v["state"] for k, v in ctl.controller.snapshot().items()}
        assert all(s in (EXPLORED, DEAD_END) for s in states.values())
```

（`run_for_test` 是 `run_task` 的最小参数包装：不触真 solver_cmd/client/journal —— 实现时把「拉节点→跑 runner→回灌 planner」的核心循环提为 `_drive(...)` 纯函数，`run_task` 与 `run_for_test` 都调它。）

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest orchestrator/tests/test_v2_controller.py -v`
Expected: ModuleNotFoundError

- [ ] **Step 3: 实现 v2_controller.py**

核心循环（伪码转真码时保持纯同步状态机 + asyncio 并发）：

```python
"""v2 主控：图探索循环（harness-v2-openhands.md §2/§3）。

并发的唯一切实点在 GraphController；本模块只负责把
planner 意图 → 节点执行（runner 注入，生产 = streams.run_solver_with_stream
包一层，测试 = FakeRunner）→ 结果回灌。HARNESS_V2=off 时本模块不被调用。
"""

from __future__ import annotations

import asyncio

from .v2_bridge import Delegate, Finish, PlannerBridge


class StubPlanner:
    """确定性假 planner：广度优先委派 UNEXPLORED 节点，全部过一遍即 Finish。"""

    def next_action(self, snapshot: dict) -> Delegate | Finish:
        for key, info in snapshot.items():
            if info["state"].value == "UNEXPLORED":
                return Delegate(key=key, directive="", parent=info["parent"])
        return Finish("all visited")


class V2Controller:
    def __init__(self, *, runner, max_parallel: int = 3,
                 node_timeout_s: float = 900.0, memory=None) -> None:
        from .v2_state import GraphController
        self.controller = GraphController(max_parallel=max_parallel,
                                          node_timeout_s=node_timeout_s)
        self.bridge = PlannerBridge(memory=memory, controller=self.controller)
        self.runner = runner

    async def _drive(self, planner, *, poll_interval: float = 0.01) -> None:
        loop = asyncio.get_running_loop()
        running: dict[str, asyncio.Task] = {}
        try:
            while True:
                # 熔断扫描
                for key in self.controller.expired():
                    self.bridge.confirm_state(key, explored=False)
                    running.pop(key, None)
                # 空槽 + planner 决策
                while self.controller.free_slots > 0:
                    action = await loop.run_in_executor(
                        None, planner.next_action, self.controller.snapshot())
                    if isinstance(action, Finish):
                        return
                    self.bridge.handle_delegate(action)
                    self.controller.begin(action.key)
                    running[action.key] = asyncio.create_task(self._run_node(action))
                if not running:
                    await asyncio.sleep(poll_interval)
                    continue
                done, _ = await asyncio.wait(
                    running.values(), timeout=poll_interval,
                    return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    key = next(k for k, task in running.items() if task is t)
                    explored = bool(t.result() and t.result()[2])
                    self.bridge.confirm_state(key, explored=explored)
                    running.pop(key)
        finally:
            for t in running.values():
                t.cancel()

    async def _run_node(self, d: Delegate):
        return await self.runner(key=d.key, directive=d.directive)

    async def run_task(self, **kw):  # 生产入口：见实现说明
        raise NotImplementedError("Task 7 Step 3 内填生产装配")

    async def run_for_test(self, *, planner) -> None:
        await self._drive(planner)
```

生产装配 `run_task`（真代码，非桩）：把 `streams.run_solver_with_stream` 包成 runner —— `key` 映射到该题 workspace 内的 `node-{key}` 子目录跑 dual_main（`SOLVER_NODE_DIRECTIVE=directive` 注入 env），`directive` 经 Task 6 `sop.load`/catalog 拼进注入文本；早收割沿用既有 `submit_candidates`。装配细节照抄 `main.py` 现有 `_pipeline` 调用点的参数来源，不改经济学。

- [ ] **Step 4: dual_main 注入位（≤10 行）**

`solver/dual_main.py` 的 APPEND_SYSTEM 组装处（`setup_orch_mode` 内，grep `APPEND_SYSTEM` 定位）追加：

```python
node_directive = os.environ.get("SOLVER_NODE_DIRECTIVE", "").strip()
if node_directive:
    append_parts.append(f"NODE FOCUS（本次委派目标，最高优先）:\n{node_directive}")
```

回归：`uv run pytest orchestrator/tests/test_dual_solver_contract.py orchestrator/tests/test_solver_contract.py -v`

- [ ] **Step 5: 全量 + lint + Commit**

Run: `uv run pytest orchestrator/tests/test_v2_controller.py orchestrator/tests/test_dual_solver_contract.py -v && make lint`
Expected: PASS

```bash
git add orchestrator/src/orchestrator/v2_controller.py orchestrator/tests/test_v2_controller.py solver/dual_main.py
git commit -m "feat(v2): 主控图探索循环 —— planner 意图→节点执行→回灌，SOLVER_NODE_DIRECTIVE 注入位"
```

---

### Task 8: e2e stub 全链路（HARNESS_V2=on）+ off 回归

**Files:**
- Create: `orchestrator/tests/test_v2_e2e.py`
- Test: `orchestrator/tests/test_e2e.py`（回归，HARNESS_V2 不设 = off）

**Interfaces:**
- Consumes: Task 7 `V2Controller.run_for_test` 路径；既有 e2e 的假 solver stub 模式（读 `test_e2e.py` 现有 fixture 复用其 SOLVER_PI_CMD 假 pi stub 写法）。
- Produces: 一个不依赖真 LLM 的 v2 全链路测试：脚本 planner → 3 节点图 → FakeRunner 产 flag → 验证闸门确认 → （模拟）submit。

- [ ] **Step 1: 写测试**

```python
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
```

- [ ] **Step 2: 跑确认失败 → Step 3: 补缺实现（预期只需修胶水，核心模块 Task 2-7 已绿）→ Step 4: 通过**

Run: `uv run pytest orchestrator/tests/test_v2_e2e.py -v && uv run pytest orchestrator/tests/test_e2e.py -v && make lint`
Expected: v2 e2e PASS；旧 e2e（off 路径）PASS 不回归

- [ ] **Step 5: Commit**

```bash
git add orchestrator/tests/test_v2_e2e.py
git commit -m "test(v2): stub planner 全链路 e2e —— 图探索→flag→验证闸门，off 路径零回归"
```

---

### Task 9: 收尾 —— 文档对齐 + 全量门禁

**Files:**
- Modify: `docs/harness-v2-openhands.md`（§7 未决 → 记录 stub e2e 绿、OpenHands 真跑冒烟仍待办）
- Modify: `docs/open-questions.md`（追加 B17: OpenHands 1.11.0 离线镜像冒烟清单）

**Interfaces:** 无新代码接口。

- [ ] **Step 1: 全量门禁**

Run: `make lint && make test`
Expected: 全绿（基线 188 + 本计划新增 ≈20 个用例）

- [ ] **Step 2: 文档落笔**

harness-v2-openhands.md §7 追加一行：`2026-10-16：stub e2e 全绿（test_v2_*），OpenHands 真跑冒烟 = B17，未绿前 HARNESS_V2 不在决赛镜像开 on`。open-questions.md 追加 B17 条目（冒烟检查项：config 目录写位 / 隐式网络探活是否 fail / 内存峰值 / 委派 Action 事件流可用性）。

- [ ] **Step 3: Commit**

```bash
git add docs/harness-v2-openhands.md docs/open-questions.md
git commit -m "docs(v2): 冒烟门禁落笔 —— B17 OpenHands 真跑冒烟为 HARNESS_V2=on 的先决项"
```

---

## 明确不在本计划内

- OpenHandsPlanner 真跑实现与冒烟（B17，e2e 绿后单独验证，验证绿了才接线 HARNESS_V2=on）
- TriviumDB Node napi 绑定（B15）
- docker/Dockerfile 的 v2 运行面改造（等冒烟结论）
- Pi 侧任何改动超出 dual_main 的 SOLVER_NODE_DIRECTIVE 注入位
