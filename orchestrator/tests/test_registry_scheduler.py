"""Registry + Scheduler 单测（architecture.md §3.2/§4.2/§4.3/§4.4）。"""

from __future__ import annotations

from orchestrator.registry import ChallengeRecord, Registry
from orchestrator.scheduler import (
    can_start,
    eligible,
    plan_phase,
    priority_key,
    slice_for,
    slice_weight,
)


def row(code: str, **kw) -> dict:
    base = {
        "unique_code": code,
        "description": None,
        "difficulty": "easy",
        "level": 1,
        "total_score": 100,
        "flag_count": 1,
        "correct_flag_count": 0,
        "is_completed": False,
        "container_status": "stopped",
        "container_addr": [],
    }
    base.update(kw)
    return base


def rec(code: str, **kw) -> ChallengeRecord:
    kw.setdefault("unique_code", code)
    return ChallengeRecord(**kw)


# ---------------------------------------------------------------- Registry ----
def test_sync_builds_records_from_platform_truth():
    reg = Registry()
    reg.sync([row("a"), row("b", is_completed=True, correct_flag_count=1)])
    assert set(reg.records) == {"a", "b"}
    assert reg.records["b"].is_completed is True
    assert reg.completed_codes() == ["b"]


def test_zombie_cleared_releases_slot():
    """平台 stopped、本地记为活跃 → 清僵尸 + 释放额度 + 重新排队。"""
    reg = Registry()
    reg.sync([row("a", container_status="available", container_addr=["1.2.3.4:80"])])
    assert reg.active_count() == 1
    reg.records["a"].state = "SOLVING"
    reg.sync([row("a")])  # 平台现在 stopped
    assert reg.active_count() == 0
    assert reg.zombies_cleared == ["a"]
    assert reg.records["a"].state == "QUEUED"


def test_orphan_detection_on_first_sync():
    reg = Registry()
    reg.sync([row("a"), row("ghost", container_status="available")])
    assert reg.orphans == ["ghost"]
    reg.sync([row("a"), row("ghost")])  # 第二次对账：ghost 已建档不再算孤儿
    assert reg.orphans == []


def test_locked_lifecycle():
    reg = Registry()
    reg.sync([row("a"), row("b", level=2)])
    reg.mark_locked("b")
    assert reg.locked_codes() == ["b"]
    assert reg.records["b"].state == "LOCKED"
    assert reg.requeue_unlocked() == ["b"]
    assert reg.locked_codes() == []
    assert reg.records["b"].state == "QUEUED"


def test_progress_truth_overwritten_each_sync():
    reg = Registry()
    reg.sync([row("a", flag_count=2)])
    assert reg.records["a"].remaining_flags == 2
    reg.sync([row("a", flag_count=2, correct_flag_count=1)])
    assert reg.records["a"].remaining_flags == 1
    assert reg.records["a"].remaining_score == 50.0


# --------------------------------------------------------------- Scheduler ----
def test_priority_easy_high_value_first():
    """性价比优先：easy 高分 > hard 高分 > easy 低分。"""
    records = {
        "hard": rec("hard", difficulty="hard", total_score=200, flag_count=1),
        "easy100": rec("easy100", difficulty="easy", total_score=100, flag_count=1),
        "easy50": rec("easy50", difficulty="easy", total_score=50, flag_count=1),
    }
    order = [r.unique_code for r in eligible(records)]
    assert order == ["easy100", "easy50", "hard"]


def test_priority_easy_not_starved_by_medium():
    """回归：easy 100 分必须排在 medium 300 分之前。

    原 cost（easy 1.0）下 easy100 性价比 100 < medium300 的 150，实测导致 6 道
    easy 送分题整轮从未被启动 —— 先易后难的纪律被成本模型反噬。"""
    records = {
        "easy100": rec("easy100", difficulty="easy", total_score=100, flag_count=1),
        "medium300": rec("medium300", difficulty="medium", total_score=300, flag_count=1),
    }
    order = [r.unique_code for r in eligible(records)]
    assert order == ["easy100", "medium300"]


def test_multi_flag_downweighted_to_back():
    """多阶段题（flag_count>1）实测零产出，按成本放大排到队尾。

    b-01（medium 1200 分 4 flag）原本性价比 600 排第一，放大后应低于
    普通 medium 300 分单 flag 题。"""
    records = {
        "b-01": rec("b-01", difficulty="medium", total_score=1200, flag_count=4),
        "medium300": rec("medium300", difficulty="medium", total_score=300, flag_count=1),
    }
    order = [r.unique_code for r in eligible(records)]
    assert order == ["medium300", "b-01"]


def test_priority_level_and_difficulty_tiebreak():
    """性价比相同时 level 升序，再 easy → hard。"""
    records = {
        "m1": rec("m1", difficulty="medium", level=1, total_score=200, flag_count=1),
        "h1": rec("h1", difficulty="hard", level=1, total_score=400, flag_count=1),
    }
    # 新契约：难度升序是主键 —— medium 永远排在 hard 之前（先拿分再说）
    assert priority_key(records["m1"])[0] < priority_key(records["h1"])[0]
    assert eligible(records)[0].unique_code == "m1"
    records["h1"].level = 2
    assert eligible(records)[0].unique_code == "m1"


def test_eligible_filters_completed_locked_active_inflight():
    records = {
        "done": rec("done", is_completed=True),
        "locked": rec("locked", locked=True),
        "active": rec("active", active_since_reconcile=True),
        "inflight": rec("inflight", state="SOLVING"),
        "closed": rec("closed", state="CLOSED"),
        "ok": rec("ok"),
    }
    assert [r.unique_code for r in eligible(records)] == ["ok"]


def test_concurrency_gate():
    assert can_start(active_count=2, concurrency_limit=3)
    assert not can_start(active_count=3, concurrency_limit=3)


def test_plan_phase1_slice_clamped():
    """TOTAL=3600，6 题，并发 3：并发系数算出 810s，但被 Phase 1 上限 420s 夹住。

    并发系数：`预算/题数` 是串行墙钟，3 路并发应 ×3（漏掉会把片压到地板）。
    Phase 1 上限：上限只决定「失败题占用槽位多久」——成功题几十秒就结束，
    压到 420s 让一个槽位 30 分钟能周转 ~4 题（实测 1785s 的片让 3 个槽位被
    b-01/b-02/b-03 独占 30 分钟）。"""
    plan = plan_phase(elapsed_s=0, total_s=3600, reserve_s=600, n_eligible=6)
    assert (plan.phase, round(plan.slice_s), plan.finalize) == (1, 420, False)
    assert plan.slice_cap_s == 420


def test_phase1_cap_bounds_failing_challenge():
    """高分多阶段题在 Phase 1 也拿不到超过上限的片（否则独占槽位）。"""
    huge = rec("b-02", difficulty="hard", total_score=1800, flag_count=6)
    plan = plan_phase(elapsed_s=0, total_s=21600, reserve_s=600, n_eligible=40, concurrency=3)
    assert slice_for(huge, plan.slice_s, slice_min_s=300, slice_max_s=plan.slice_cap_s) == 420


def test_plan_phase1_scales_with_concurrency():
    """并发越高，每题墙钟片越长（同一份预算摊给同样多的题）。"""
    lo = plan_phase(elapsed_s=0, total_s=3600, reserve_s=600, n_eligible=20, concurrency=1)
    hi = plan_phase(elapsed_s=0, total_s=3600, reserve_s=600, n_eligible=20, concurrency=3)
    assert hi.slice_s > lo.slice_s


# ---- 分数加权（§4.3 修订）----
def test_slice_weight_orders_by_remaining_score():
    """高分题权重更大；1800 分题不拿 6 倍（sqrt 缩放 + 上限 2.5）。"""
    low = rec("low", difficulty="easy", total_score=100, flag_count=1)
    mid = rec("mid", difficulty="medium", total_score=300, flag_count=1)
    high = rec("high", difficulty="medium", total_score=1200, flag_count=4)
    huge = rec("huge", difficulty="hard", total_score=1800, flag_count=6)
    assert slice_weight(low) < slice_weight(mid) < slice_weight(high) <= slice_weight(huge)
    assert slice_weight(huge) <= 2.5  # 上限，避免吃光预算


def test_slice_for_clamps_to_bounds():
    big = rec("big", difficulty="hard", total_score=1800, flag_count=6)
    assert slice_for(big, 3000.0, slice_min_s=120, slice_max_s=1800) == 1800
    small = rec("small", difficulty="easy", total_score=100, flag_count=1)
    assert slice_for(small, 1.0, slice_min_s=120, slice_max_s=1800) == 120


def test_plan_phase1_slice_floored_at_min():
    plan = plan_phase(elapsed_s=0, total_s=3600, reserve_s=600, n_eligible=100)
    assert plan.slice_s >= 120


def test_plan_finalize_at_reserve():
    """剩余 < RESERVE → 收尾保护。"""
    plan = plan_phase(elapsed_s=3500, total_s=3600, reserve_s=600, n_eligible=3)
    assert plan.finalize is True


def test_plan_phase2_after_phase1_window():
    plan = plan_phase(elapsed_s=1700, total_s=3600, reserve_s=600, n_eligible=3)
    assert (plan.phase, plan.finalize) == (2, False)
    assert plan.slice_s >= 120


def test_slice_for_sweep_mode_floors_by_difficulty():
    """极速扫题（Phase1 cap=45）：easy 保底 90s、hard/多 flag 保底 120s、其余 30-45s。"""
    easy = rec("e", difficulty="easy", total_score=100, flag_count=1)
    medium = rec("m", difficulty="medium", total_score=300, flag_count=1)
    hard = rec("h", difficulty="hard", total_score=500, flag_count=1)
    assert slice_for(easy, 45, slice_min_s=30, slice_max_s=45) == 90
    assert slice_for(hard, 45, slice_min_s=30, slice_max_s=45) == 120
    assert slice_for(medium, 45, slice_min_s=30, slice_max_s=45) <= 45


def test_short_slice_gate_skips_fresh_hard_task():
    from orchestrator.scheduler import should_start

    assert should_start(rec("t", difficulty="hard"), 80, has_progress=False, gate=True) is False
    assert should_start(rec("t", difficulty="hard"), 200, has_progress=False, gate=True) is True


def test_short_slice_gate_exempts_easy_and_progress():
    from orchestrator.scheduler import should_start

    assert should_start(rec("t", difficulty="easy"), 80, has_progress=False, gate=True) is True
    assert should_start(rec("t", difficulty="hard"), 80, has_progress=True, gate=True) is True
    assert should_start(rec("t", difficulty="hard"), 80, has_progress=False, gate=False) is True


def test_empty_slice_env_falls_back_to_defaults(monkeypatch):
    import importlib

    import orchestrator.scheduler as scheduler

    monkeypatch.setenv("PHASE1_SLICE_MAX_S", "")
    monkeypatch.setenv("MIN_USEFUL_SLICE_S", "")
    reloaded = importlib.reload(scheduler)
    assert reloaded.PHASE1_SLICE_MAX_S == 420
    assert reloaded.MIN_USEFUL_SLICE_S == 150


def test_attempted_easy_demoted_below_unattempted_medium():
    """attempt-aware：被 probe_stop 过的 easy 不再压住未跑过的 medium。"""
    records = {
        "easy_tried": rec("easy_tried", difficulty="easy", total_score=100, attempts=1),
        "medium_new": rec("medium_new", difficulty="medium", total_score=1200),
        "easy_new": rec("easy_new", difficulty="easy", total_score=100),
    }
    order = [r.unique_code for r in eligible(records)]
    assert order == ["easy_new", "medium_new", "easy_tried"]


def test_attempts_cap_at_two():
    """attempts 再大也只降两级，避免已失败题永久沉底。"""
    tried = rec("tried", difficulty="easy", total_score=100, attempts=9)
    hard_new = rec("hard_new", difficulty="hard", total_score=100)
    assert priority_key(tried)[0] <= priority_key(hard_new)[0] + 1
