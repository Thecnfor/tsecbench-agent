"""Scheduler —— 优先级排序、时间片分配、并发闸门、收尾保护（architecture.md §4）。

排序直觉：**先易后难，快速落袋**（§4.2）。hard 题只用来填空档或 Phase 2 深挖。
纯函数为主，便于单测；并发闸门由主循环配合 Registry.active_count() 使用。
"""

from __future__ import annotations

from dataclasses import dataclass

from .registry import ChallengeRecord

DIFFICULTY_RANK = {"easy": 0, "medium": 1, "hard": 2}
# 预估成本映射（§4.2 性价比的分母）：难度为主，level 加权
_DIFF_COST = {"easy": 1.0, "medium": 2.0, "hard": 4.0}

PHASE1_BUDGET_RATIO = 0.45  # Phase 1 广撒网占总时限比例（§4.3）
SLICE_MIN_S = 120
SLICE_MAX_S = 1200


def estimated_cost(rec: ChallengeRecord) -> float:
    return _DIFF_COST.get(rec.difficulty, 2.0) * (1.0 + 0.1 * max(0, rec.level - 1))


def priority_key(rec: ChallengeRecord) -> tuple[float, int, int, str]:
    """排序键（§4.2，从高到低）：

    1. is_completed == false（已通关直接跳过，在 eligible 里已过滤）
    2. 不在 LOCKED 集合（locked 在 eligible 已过滤）
    3. 性价比 = 剩余可得分数 / 预估成本（降序 → 取负）
    4. level 升序、difficulty easy → hard
    """
    value = rec.remaining_score / estimated_cost(rec)
    return (-value, rec.level, DIFFICULTY_RANK.get(rec.difficulty, 1), rec.unique_code)


def eligible(records: dict[str, ChallengeRecord]) -> list[ChallengeRecord]:
    """可启动的题：未通关、未 LOCKED、不在终态、本地无活跃容器。"""
    out = []
    for rec in records.values():
        if rec.is_completed or rec.locked:
            continue
        if rec.state in ("SOLVED", "ABANDONED", "CLOSED", "CLOSING"):
            continue
        if rec.active_since_reconcile:
            continue
        if rec.state in ("STARTING", "POLLING", "SOLVING", "HARVESTING", "SUBMITTING"):
            continue  # 已在途
        out.append(rec)
    return sorted(out, key=priority_key)


@dataclass(slots=True)
class PhasePlan:
    phase: int  # 1 广撒网 / 2 深挖
    slice_s: float  # 每题时间片
    finalize: bool  # 进入收尾保护


def plan_phase(
    *,
    elapsed_s: float,
    total_s: int,
    reserve_s: int,
    n_eligible: int,
    remaining_score: float = 0.0,
    slice_min_s: float = SLICE_MIN_S,
    slice_max_s: float = SLICE_MAX_S,
) -> PhasePlan:
    """两阶段预算与收尾保护（§4.3）。

    - 剩余 < RESERVE → finalize（停止启动新题、杀 solver、强制提交与 close）；
    - elapsed < 0.45×TOTAL → Phase 1，每题时间片 = 0.45×TOTAL / 题数（夹在上下限）；
    - 之后 Phase 2，剩余预算按进展度重投（这里给一个统一的大时间片）。
    """
    remaining = total_s - elapsed_s
    if remaining <= reserve_s:
        return PhasePlan(phase=2, slice_s=0.0, finalize=True)
    phase1_total = PHASE1_BUDGET_RATIO * total_s
    if elapsed_s < phase1_total and n_eligible > 0:
        slice_s = max(slice_min_s, min(slice_max_s, phase1_total / n_eligible))
        # Phase 1 末尾预留过渡：时间片不得超过 phase1 剩余
        slice_s = min(slice_s, max(slice_min_s, phase1_total - elapsed_s))
        return PhasePlan(phase=1, slice_s=slice_s, finalize=False)
    # Phase 2：剩余预算摊给还有剩余分数的题；无剩余分数给最小片
    remaining_budget = remaining - reserve_s
    slice_s = max(slice_min_s, min(slice_max_s * 2, remaining_budget / max(1, n_eligible)))
    if remaining_score <= 0:
        slice_s = slice_min_s
    return PhasePlan(phase=2, slice_s=slice_s, finalize=False)


def can_start(*, active_count: int, concurrency_limit: int) -> bool:
    """并发闸门（§4.4）：活跃容器数 < 上限才允许 start。"""
    return active_count < concurrency_limit
