"""并发槽计数：只认在飞 pipeline，不能与 registry 对账视图相加。"""

from __future__ import annotations

from types import SimpleNamespace

from orchestrator.main import _active_slots


def test_active_slots_counts_running_pipelines_only():
    ctx = SimpleNamespace(running={"a", "b"}, registry=SimpleNamespace(active_count=lambda: 99))
    assert _active_slots(ctx) == 2


def test_active_slots_frees_when_pipeline_finishes():
    running = {"a", "b", "c"}
    ctx = SimpleNamespace(running=running, registry=SimpleNamespace(active_count=lambda: 3))
    running.remove("a")
    assert _active_slots(ctx) == 2


def test_should_extend_rules():
    from orchestrator.main import _should_extend

    assert _should_extend(240, False, True) is True
    assert _should_extend(0, False, True) is False
    assert _should_extend(240, True, True) is False
    assert _should_extend(240, False, False) is False
