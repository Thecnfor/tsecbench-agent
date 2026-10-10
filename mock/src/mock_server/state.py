"""mock server 的内存态与题面加载。

设计要点（runbook.md §4.1 + architecture.md §7）：
- 题面（含 flag 真值、故障注入序列）可由 `MOCK_CHALLENGES_JSON` 指定的 JSON 文件注入，
  便于单测与 e2e 构造任意场景；默认内置 4 题。
- 对外 API 形状严格按本届文档（platform-api.md §5）：裸 JSON、`unique_code`、
  `BENCHMARK_TOKEN` 头；**故意不提供** `hint_cost_radio` / `hint_viewed`，
  让编排器在 mock 阶段就习惯「hint 状态只能自己记」。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

CONCURRENCY_LIMIT = 3


@dataclass
class ChallengeSpec:
    """一道题的 mock 题面。`flags` 是真值，绝不通过 API 暴露。"""

    unique_code: str
    description: str | None = None
    difficulty: str = "easy"
    level: int = 1
    total_score: int = 100
    flags: list[str] = field(default_factory=list)
    start_delay_s: float = 0.0  # POST /start 后 pending → available 的延迟
    # 故障注入序列：接口名 → 依序消费的注入项。
    # 注入项：`slow:N`（阻塞 N 秒）| `500` | `503` | `reset`（断连）| `garbage`（畸形响应体）| `ok`
    # 序列中最后一项会被无限重复（写 `ok` 表示恢复），单元素序列即持续故障。
    faults: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict) -> ChallengeSpec:
        return cls(
            unique_code=str(raw["unique_code"]),
            description=raw.get("description"),
            difficulty=str(raw.get("difficulty", "easy")),
            level=int(raw.get("level", 1)),
            total_score=int(raw.get("total_score", 100)),
            flags=[str(f) for f in raw.get("flags", [])],
            start_delay_s=float(raw.get("start_delay_s", 0)),
            faults={k: [str(x) for x in v] for k, v in raw.get("faults", {}).items()},
        )


def default_specs() -> list[ChallengeSpec]:
    """内置 4 题：覆盖单 flag / 多 flag / 延迟启动 / 高关卡（配 MOCK_LEVEL_GATE 用）。"""
    return [
        ChallengeSpec(
            "mock_web_01",
            description="web 题：登录页存在 SQL 注入，读取 flag",
            flags=["FLAG{mock_web_flag_a}"],
        ),
        ChallengeSpec(
            "mock_pwn_01",
            description="pwn 题：缓冲区溢出",
            flags=["FLAG{mock_pwn_flag_a}", "FLAG{mock_pwn_flag_b}"],
        ),
        ChallengeSpec(
            "mock_delay_01",
            description="容器启动慢的题（验证轮询时序）",
            flags=["FLAG{mock_delay_flag}"],
            start_delay_s=2.0,
        ),
        ChallengeSpec(
            "mock_hard_01",
            description="hard 题（配 MOCK_LEVEL_GATE 验证 403）",
            difficulty="hard",
            level=2,
            flags=["FLAG{mock_hard_flag}"],
        ),
    ]


def load_specs() -> list[ChallengeSpec]:
    path = os.environ.get("MOCK_CHALLENGES_JSON", "")
    if not path:
        return default_specs()
    raw = json.loads(open(path, encoding="utf-8").read())  # noqa: PTH123, SIM115 - 允许相对路径
    return [ChallengeSpec.from_dict(item) for item in raw]


class MockState:
    """平台侧内存态。线程安全：ThreadingHTTPServer 多线程访问，锁保护全部写路径。"""

    def __init__(
        self,
        specs: list[ChallengeSpec],
        *,
        current_level: int = 1,
        rate_limit: int = 0,
        total_limit_s: float | None = None,
    ) -> None:
        self.lock = threading.RLock()
        self.current_level = current_level
        self.rate_limit = rate_limit
        self.total_limit_s = total_limit_s
        self.opened_at = time.monotonic()
        self.recent_requests: list[float] = []
        # (unique_code, flag) → 已正确提交集合（duplicate 的判定真值）
        self.correct_submitted: set[tuple[str, str]] = set()
        self.submitted_any: set[tuple[str, str]] = set()  # 提交过（含错误）
        self.hint_viewed: set[str] = set()  # 平台读不回来，mock 内部记账用
        self.live: dict[str, dict] = {}
        for spec in specs:
            self.live[spec.unique_code] = {
                "spec": spec,
                "container_status": "stopped",
                "pending_since": None,
                "container_addr": [],
            }

    # ---- 时间相关 ------------------------------------------------------------
    def task_expired(self) -> bool:
        if self.total_limit_s is None:
            return False
        return time.monotonic() - self.opened_at > self.total_limit_s

    def _refresh_container(self, entry: dict) -> None:
        """pending → available 时序：start 后经过 start_delay_s 才 available，
        且 container_addr 只在 available 时非空（platform-api.md §4.1）。"""
        if entry["container_status"] != "pending":
            return
        elapsed = time.monotonic() - entry["pending_since"]
        if elapsed >= entry["spec"].start_delay_s:
            entry["container_status"] = "available"
            idx = sorted(self.live).index(entry["spec"].unique_code)
            entry["container_addr"] = [f"127.0.0.1:{9100 + idx}"]

    # ---- 对外视图 ------------------------------------------------------------
    def challenge_view(self, entry: dict) -> dict:
        """GET /challenges 元素形状，严格按 platform-api.md §5.1。"""
        spec: ChallengeSpec = entry["spec"]
        with self.lock:
            self._refresh_container(entry)
            correct = sum(1 for f in spec.flags if (spec.unique_code, f) in self.correct_submitted)
            return {
                "unique_code": spec.unique_code,
                "description": spec.description,
                "difficulty": spec.difficulty,
                "level": spec.level,
                "total_score": spec.total_score,
                "flag_count": len(spec.flags),
                "correct_flag_count": correct,
                "is_completed": len(spec.flags) > 0 and correct == len(spec.flags),
                "container_status": entry["container_status"],
                "container_addr": list(entry["container_addr"]),
            }

    def list_challenges(self) -> list[dict]:
        with self.lock:
            return [self.challenge_view(self.live[c]) for c in sorted(self.live)]

    def active_count(self) -> int:
        return sum(
            1
            for e in self.live.values()
            if e["container_status"] in ("pending", "available", "stop_pending")
        )

    # ---- 限频（MOCK_RATE_LIMIT：上届语义，每秒 N 次、所有接口共享） ----------------
    def check_rate_limit(self) -> bool:
        if self.rate_limit <= 0:
            return True
        now = time.monotonic()
        with self.lock:
            self.recent_requests = [t for t in self.recent_requests if now - t < 1.0]
            if len(self.recent_requests) >= self.rate_limit:
                return False
            self.recent_requests.append(now)
            return True

    # ---- fault 注入：消费该题该接口的下一个注入项 -------------------------------
    def pop_fault(self, unique_code: str, interface: str) -> str | None:
        with self.lock:
            entry = self.live.get(unique_code)
            if entry is None:
                return None
            seq = entry["spec"].faults.get(interface) or []
            if not seq:
                return None
            return seq.pop(0) if len(seq) > 1 else seq[0]


def build_state(
    specs: list[ChallengeSpec] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> MockState:
    """从环境变量（或显式参数）构造 state。供 __main__ 与测试共用。"""
    env = env if env is not None else os.environ
    return MockState(
        specs if specs is not None else load_specs(),
        current_level=int(env.get("MOCK_CURRENT_LEVEL", "1")),
        rate_limit=int(env.get("MOCK_RATE_LIMIT", "0")),
        total_limit_s=float(env["MOCK_TOTAL_LIMIT_S"]) if env.get("MOCK_TOTAL_LIMIT_S") else None,
    )
