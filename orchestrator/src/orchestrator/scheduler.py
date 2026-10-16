"""Scheduler —— 优先级排序、时间片分配、并发闸门、收尾保护（architecture.md §4）。

排序直觉：**先易后难，快速落袋**（§4.2）。hard 题只用来填空档或 Phase 2 深挖。
纯函数为主，便于单测；并发闸门由主循环配合 Registry.active_count() 使用。
"""

from __future__ import annotations

from dataclasses import dataclass

from .registry import ChallengeRecord

DIFFICULTY_RANK = {"easy": 0, "medium": 1, "hard": 2}
# 预估成本映射（§4.2 性价比的分母）：难度为主，level 加权。
# easy 从 1.0 降到 0.6（§4.2 修订）：原值下 easy 100 分的性价比（100）低于
# medium 300（150）与 hard 500（125），实测 63 题里 6 道 easy 送分题（共 600 分）
# 排到队尾、整轮从未启动。easy 题接近必解，按「每分每秒的期望得分」应当先做。
_DIFF_COST = {"easy": 0.6, "medium": 2.0, "hard": 4.0}

PHASE1_BUDGET_RATIO = 0.45  # Phase 1 广撒网占总时限比例（§4.3）
SLICE_MIN_S = 120
SLICE_MAX_S = 1200
# Phase 1 单题时间片上限（§4.3 修订）：上限只决定「**失败题占用槽位多久**」——
# 成功题几十秒就结束，上限对它们无影响。实测 1785s 的片让 3 个槽位被
# b-01/b-02/b-03 连续独占 30 分钟，其余 60 题全部排队。压到 420s 后，一个槽位
# 30 分钟能周转 ~4 题。Phase 2 深挖不受此限（用 SLICE_MAX_S）。
PHASE1_SLICE_MAX_S = 420

# 时间片按剩余分数加权（§4.3 修订）：多阶段高分题（b-01 1200 / b-02 1800）
# 与 200 分单题拿同样的片，实测 300s 下必死 —— 用 sqrt 缩放，避免 1800 分题
# 拿到 6 倍片把预算吃光。
SCORE_REF = 300.0  # 加权基准分：300 分题权重 = 1.0
WEIGHT_FLOOR = 0.7
WEIGHT_CEIL = 2.5


# 多 flag（多阶段）题的额外成本系数（§4.2 修订）：实测 b-01/b-02/b-03（4/6/4 flag）
# 试过 3 轮零产出，且每题要串联多个 flag 才算通关 —— 按 flag_count 放大预估成本，
# 把它们排到队尾，先把单 flag 的题扫完（用户决策：得分优先）。
_MULTI_FLAG_COST = 1.5


def estimated_cost(rec: ChallengeRecord) -> float:
    base = _DIFF_COST.get(rec.difficulty, 2.0) * (1.0 + 0.1 * max(0, rec.level - 1))
    return base * (1.0 + _MULTI_FLAG_COST * max(0, rec.flag_count - 1))


def slice_weight(rec: ChallengeRecord) -> float:
    """按剩余分数给时间片加权（sqrt 缩放 + 上下夹）。"""
    if rec.remaining_score <= 0:
        return WEIGHT_FLOOR
    weight = (rec.remaining_score / SCORE_REF) ** 0.5
    return max(WEIGHT_FLOOR, min(WEIGHT_CEIL, weight))


def slice_for(
    rec: ChallengeRecord,
    base_slice: float,
    *,
    slice_min_s: float = SLICE_MIN_S,
    slice_max_s: float = SLICE_MAX_S,
) -> float:
    """把 phase 基础片按该题分数加权，夹回 [min, max]。"""
    return max(slice_min_s, min(slice_max_s, base_slice * slice_weight(rec)))


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
    slice_s: float  # 每题时间片（本 phase 的基准）
    finalize: bool  # 进入收尾保护
    slice_cap_s: float = SLICE_MAX_S  # 本 phase 的单题上限（分数加权后夹到这里）


def plan_phase(
    *,
    elapsed_s: float,
    total_s: int,
    reserve_s: int,
    n_eligible: int,
    remaining_score: float = 0.0,
    slice_min_s: float = SLICE_MIN_S,
    slice_max_s: float = SLICE_MAX_S,
    concurrency: int = 3,
) -> PhasePlan:
    """两阶段预算与收尾保护（§4.3）。

    - 剩余 < RESERVE → finalize（停止启动新题、杀 solver、强制提交与 close）；
    - elapsed < 0.45×TOTAL → Phase 1，每题时间片 = 预算 × 并发 / 题数（夹在上下限）；
    - 之后 Phase 2，剩余预算按进展度重投（这里给一个统一的大时间片）。

    **并发系数是必须的**（§4.3 修订）：`预算 / 题数` 算的是「串行墙钟」，
    但同一时刻有 `concurrency` 个 solver 在跑，每题实际可用墙钟是其 `concurrency` 倍。
    漏掉这个系数会让每题少拿 3 倍片 —— 实测把 63 题的片全压到 300s 地板，
    23 次超时里一大半是「差一点」被砍死的。
    """
    remaining = total_s - elapsed_s
    if remaining <= reserve_s:
        return PhasePlan(phase=2, slice_s=0.0, finalize=True)
    conc = max(1, concurrency)
    phase1_total = PHASE1_BUDGET_RATIO * total_s
    if elapsed_s < phase1_total and n_eligible > 0:
        cap1 = min(slice_max_s, PHASE1_SLICE_MAX_S)
        slice_s = max(slice_min_s, min(cap1, phase1_total * conc / n_eligible))
        # Phase 1 末尾预留过渡：时间片不得超过 phase1 剩余
        slice_s = min(slice_s, max(slice_min_s, phase1_total - elapsed_s))
        return PhasePlan(phase=1, slice_s=slice_s, finalize=False, slice_cap_s=cap1)
    # Phase 2：剩余预算摊给还有剩余分数的题；无剩余分数给最小片
    remaining_budget = remaining - reserve_s
    slice_s = max(slice_min_s, min(slice_max_s * 2, remaining_budget * conc / max(1, n_eligible)))
    if remaining_score <= 0:
        slice_s = slice_min_s
    return PhasePlan(phase=2, slice_s=slice_s, finalize=False, slice_cap_s=slice_max_s * 2)


def can_start(*, active_count: int, concurrency_limit: int) -> bool:
    """并发闸门（§4.4）：活跃容器数 < 上限才允许 start。"""
    return active_count < concurrency_limit
