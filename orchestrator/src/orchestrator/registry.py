"""Challenge Registry —— 平台快照对账（architecture.md §3 Challenge Registry）。

**平台状态永远是真值来源**，本地状态只作优化：
  - 每次对账用 `GET /challenges` 的行覆盖进度真值（correct_flag_count / is_completed /
    container_status / container_addr）；
  - 平台 stopped 但本地记为活跃 → 清僵尸条目，释放并发额度（§4.4）；
  - 平台 available 但本地无记录 → 孤儿容器，交上层接管或 close（§3.2）；
  - 维护 LOCKED 集合：收到 403 的题不再 start，解锁后重新入队（§5.1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 单题状态机（architecture.md §3.1）。CLOSING→CLOSED 是必经终态；
# LOCKED 是唯一不占并发额度、不需 close 的状态。
STATES = (
    "DISCOVERED",
    "QUEUED",
    "STARTING",
    "POLLING",
    "SOLVING",
    "HARVESTING",
    "SUBMITTING",
    "SOLVED",
    "RETRY",
    "ABANDONED",
    "LOCKED",
    "CLOSING",
    "CLOSED",
)

_TERMINAL = ("SOLVED", "ABANDONED", "CLOSED")
_ACTIVE_CONTAINER = ("pending", "available", "stop_pending")


@dataclass(slots=True)
class ChallengeRecord:
    """一道题的本地视图。进度字段以平台对账为准，其余是本地优化。"""

    unique_code: str
    state: str = "DISCOVERED"
    # 平台真值（对账覆盖）
    is_completed: bool = False
    correct_flag_count: int = 0
    flag_count: int = 1
    total_score: int = 0
    difficulty: str = "easy"
    level: int = 1
    container_status: str = "stopped"
    container_addr: list[str] = field(default_factory=list)
    description: str | None = None
    # 本地记账（平台读不回来的状态）
    locked: bool = False  # 收到过 403
    attempts: int = 0
    submitted: set[str] = field(default_factory=set)  # 已提交过的候选
    rejected_candidates: int = 0  # 平台判错的候选数（诱饵/错误 flag）
    matched_indices: set[int] = field(default_factory=set)
    active_since_reconcile: bool = False  # 本地认为该题容器活跃
    # 本地修复记账（EVOSTER，harness-v2.md §5；平台读不回来，不在对账路径）
    repair_widened: bool = False  # widen 动作最多一次
    next_slice_s: float | None = None  # 下轮时间片覆盖（widen 后生效一次即清零）
    # 槽位经济学：本 pass 无新 flag 且无新事实的次数。>0 的题排到队尾，
    # 且不再与非零进展题抢补位槽（对齐 CyberPenda 的零进展 pass 规则）。
    zero_progress_passes: int = 0
    reject_budget: int = 3  # 本 attempt 最多接受的平台拒绝次数

    @property
    def remaining_flags(self) -> int:
        return max(0, self.flag_count - self.correct_flag_count)

    @property
    def remaining_score(self) -> float:
        if self.flag_count <= 0:
            return 0.0
        return self.total_score * self.remaining_flags / self.flag_count


class Registry:
    """全部题目的本地视图 + 平台对账。"""

    def __init__(self) -> None:
        self.records: dict[str, ChallengeRecord] = {}
        self.orphans: list[str] = []  # 平台活跃但本地无记录的题（§3.2 第 3 条）
        self.zombies_cleared: list[str] = []  # 最近一次对账清掉的僵尸

    # ---- 对账 ----------------------------------------------------------------
    def sync(self, rows: list[dict]) -> None:
        """以平台真值覆盖本地视图（§3.2 崩溃恢复的核心步骤）。"""
        self.zombies_cleared = []
        # 孤儿：平台活跃、本地（sync 前）无记录 —— 重启后的可用容器，交上层接管或 close
        known = set(self.records)
        self.orphans = [
            row["unique_code"]
            for row in rows
            if row["unique_code"] not in known and row.get("container_status") in _ACTIVE_CONTAINER
        ]
        for row in rows:
            code = row["unique_code"]
            rec = self.records.get(code)
            if rec is None:
                rec = ChallengeRecord(unique_code=code)
                self.records[code] = rec
            rec.is_completed = bool(row.get("is_completed"))
            rec.correct_flag_count = int(row.get("correct_flag_count", 0))
            rec.flag_count = int(row.get("flag_count", 0))
            rec.total_score = int(row.get("total_score", 0))
            rec.difficulty = str(row.get("difficulty", "easy"))
            rec.level = int(row.get("level", 1))
            rec.container_status = str(row.get("container_status", "stopped"))
            rec.container_addr = list(row.get("container_addr") or [])
            rec.description = row.get("description")
            # 僵尸清理：平台已 stopped，本地却记为活跃
            if rec.container_status == "stopped" and rec.active_since_reconcile:
                rec.active_since_reconcile = False
                if rec.state in ("STARTING", "POLLING", "SOLVING"):
                    rec.state = "QUEUED"  # 容器没了但题没解完，重新排队
                self.zombies_cleared.append(code)
            # 平台活跃 → 同步本地活跃标记
            if rec.container_status in _ACTIVE_CONTAINER:
                rec.active_since_reconcile = True

    # ---- LOCKED 集合（§5.1）----------------------------------------------------
    def mark_locked(self, unique_code: str) -> None:
        rec = self.records[unique_code]
        rec.locked = True
        rec.state = "LOCKED"

    def locked_codes(self) -> list[str]:
        return [c for c, r in self.records.items() if r.locked and not r.is_completed]

    def requeue_unlocked(self) -> list[str]:
        """解锁信号后重新入队：submit 成功触发解锁 → LOCKED 题回到 QUEUED。

        简化决策：解锁可能来自任意一题的正确提交，无法精确知道解锁了哪一关，
        因此每次正确提交后把所有 LOCKED 题交回上层重新探测（start 撞 403 再锁回去
        的成本是一轮往返，可接受；漏掉解锁机会的成本是丢分）。
        """
        requeued = []
        for code, rec in self.records.items():
            if rec.locked and not rec.is_completed:
                rec.locked = False
                rec.state = "QUEUED"
                requeued.append(code)
        return requeued

    # ---- 查询 ----------------------------------------------------------------
    def active_count(self) -> int:
        return sum(1 for r in self.records.values() if r.active_since_reconcile)

    def completed_codes(self) -> list[str]:
        return [c for c, r in self.records.items() if r.is_completed]

    def droppable(self, unique_code: str) -> bool:
        """challenge_not_found 时该题应被剔除：不在已通关集合里也直接删档。"""
        return unique_code in self.records
