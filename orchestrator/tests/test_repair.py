"""EVOSTER 在线修复策略单测（harness-v2.md §5）。

信号 → 动作映射、widen 预算闸、锚定权重上下文、平票优先序 —— 全部确定性。
"""

from __future__ import annotations

from orchestrator.registry import ChallengeRecord
from orchestrator.repair import BASE_WEIGHTS, RepairPlan, choose_repair, update_weights
from orchestrator.supervisor import SolverOutcome


def make_rec() -> ChallengeRecord:
    return ChallengeRecord(unique_code="r1", flag_count=1, total_score=100)


def make_outcome(*, exit_code: int | None = 0, timed_out: bool = False) -> SolverOutcome:
    return SolverOutcome(
        exit_code=exit_code,
        timed_out=timed_out,
        duration_s=100.0,
        stdout_path=None,  # type: ignore[arg-type]
        stderr_path=None,  # type: ignore[arg-type]
    )


def _params(**kw):
    base = {"total_remaining_s": 100000.0, "slice_max_s": 1200.0, "weights": dict(BASE_WEIGHTS)}
    base.update(kw)
    return base


def _choose(rec, outcome, slice_s=300.0, **kw):
    return choose_repair(rec, outcome, slice_s, **_params(**kw))


def test_timeout_first_time_widens():
    rec = make_rec()
    plan = choose_repair(rec, make_outcome(exit_code=124, timed_out=True), 300.0, **_params())
    assert plan.action == "widen"
    assert plan.next_slice_s == 450.0  # 300 × 1.5
    assert "runway" in plan.note


def test_timeout_budget_blocks_widen():
    rec = make_rec()
    plan = choose_repair(
        rec,
        make_outcome(exit_code=124, timed_out=True),
        800.0,
        **_params(total_remaining_s=900.0),  # 放大到 1200 > 剩余 900 → widen 不可行
    )
    assert plan.action == "recompile"
    assert plan.next_slice_s is None


def test_timeout_second_time_recompiles():
    rec = make_rec()
    rec.repair_widened = True  # widen 只许一次
    plan = choose_repair(rec, make_outcome(exit_code=124, timed_out=True), 300.0, **_params())
    assert plan.action == "recompile"
    assert plan.next_slice_s is None


def test_crash_maps_to_retry_same():
    rec = make_rec()
    plan = choose_repair(rec, make_outcome(exit_code=3), 300.0, **_params())
    assert plan.action == "retry_same"
    assert plan.next_slice_s is None


def test_clean_finish_zero_candidates_recompiles():
    rec = make_rec()
    plan = choose_repair(rec, make_outcome(), 300.0, **_params())
    assert plan.action == "recompile"
    assert "less-probed" in plan.note


def test_weights_break_tie_by_priority():
    """widen 与 recompile 平票 → 固定优先序 widen 胜出（确定性）。"""
    weights = {k: 3.0 for k in BASE_WEIGHTS}
    plan = _choose(make_rec(), make_outcome(exit_code=124, timed_out=True), weights=weights)
    assert plan.action == "widen"

    rec2 = make_rec()
    rec2.repair_widened = True  # widen 退出候选 → recompile
    plan2 = _choose(rec2, make_outcome(exit_code=124, timed_out=True), weights=weights)
    assert plan2.action == "recompile"


def test_weights_prefer_higher_weight_option():
    weights = dict(BASE_WEIGHTS, widen=0.1, recompile=2.0)
    plan = _choose(make_rec(), make_outcome(exit_code=124, timed_out=True), weights=weights)
    assert plan.action == "recompile"


def test_update_weights_up_and_down():
    weights = dict(BASE_WEIGHTS)
    update_weights(weights, "widen", success=True)
    assert weights["widen"] == 1.25
    update_weights(weights, "widen", success=False)
    assert abs(weights["widen"] - 1.25 * 0.8) < 1e-9
    update_weights(weights, "unknown", success=True)  # 未知动作不进账
    assert "unknown" not in weights


def test_repair_plan_defaults() -> None:
    plan = RepairPlan(action="recompile")
    assert plan.next_slice_s is None and plan.note == ""
