"""Registry + Scheduler 单测（architecture.md §3.2/§4.2/§4.3/§4.4）。"""

from __future__ import annotations

from orchestrator.registry import ChallengeRecord, Registry
from orchestrator.scheduler import can_start, eligible, plan_phase, priority_key


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


def test_priority_level_and_difficulty_tiebreak():
    """性价比相同时 level 升序，再 easy → hard。"""
    records = {
        "m1": rec("m1", difficulty="medium", level=1, total_score=200, flag_count=1),
        "h1": rec("h1", difficulty="hard", level=1, total_score=400, flag_count=1),
    }
    # cost: medium 2, hard 4 → value 都是 100 → 平局 → unique_code 字典序
    assert priority_key(records["m1"])[:2] == priority_key(records["h1"])[:2]
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
    """TOTAL=3600，6 题：phase1 预算 1620s，每题片 270s（在 120~1200 内）。"""
    plan = plan_phase(elapsed_s=0, total_s=3600, reserve_s=600, n_eligible=6)
    assert (plan.phase, round(plan.slice_s), plan.finalize) == (1, 270, False)


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
