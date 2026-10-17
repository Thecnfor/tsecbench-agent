"""零进展 pass 的槽位经济学：沉底、不补位、全零时才允许重投。"""

from __future__ import annotations

from orchestrator.registry import ChallengeRecord
from orchestrator.scheduler import eligible, priority_key


def _rec(code: str, *, zero: int = 0, score: int = 300) -> ChallengeRecord:
    return ChallengeRecord(
        unique_code=code,
        state="QUEUED",
        total_score=score,
        flag_count=1,
        correct_flag_count=0,
        zero_progress_passes=zero,
    )


def test_zero_progress_is_filtered_while_fresh_candidates_exist():
    fresh = _rec("a-01")
    zero = _rec("a-02", zero=2, score=1800)
    out = eligible({"a-01": fresh, "a-02": zero})
    assert [rec.unique_code for rec in out] == ["a-01"]


def test_zero_progress_allowed_when_nothing_else_is_left():
    zero = _rec("a-02", zero=1)
    out = eligible({"a-02": zero})
    assert [rec.unique_code for rec in out] == ["a-02"]


def test_zero_progress_sorts_after_fresh_even_with_more_score():
    fresh = _rec("a-01", score=100)
    zero = _rec("a-05", zero=1, score=2000)
    assert priority_key(fresh) < priority_key(zero)
