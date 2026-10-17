"""并发槽计数：只认在飞 pipeline，不能与 registry 对账视图相加。"""

from __future__ import annotations

import json
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


def test_probe_decision_tiers():
    from orchestrator.main import _probe_decision

    assert _probe_decision(1, True, False, 240) == "second"
    assert _probe_decision(1, False, False, 240) == "stop"
    assert _probe_decision(2, True, False, 240) == "full"
    assert _probe_decision(2, True, True, 240) == "full"
    assert _probe_decision(3, True, False, 240) == "full"
    assert _probe_decision(1, True, True, 0) == "stop"


def test_pass_progress_only_counts_flag_candidates(tmp_path):
    from orchestrator.main import _pass_progress

    ws = tmp_path / "ws"
    (ws / "out").mkdir(parents=True)
    (ws / "out" / "prior.json").write_text(
        json.dumps({"credentials": ["Passw0rd!123"], "assets": ["10.0.0.1:80"]}),
        encoding="utf-8",
    )
    report = SimpleNamespace(new_matches=0)
    assert _pass_progress(report, 0, ws) is False
    (ws / "out" / "flags").mkdir()
    (ws / "out" / "flags" / "000.txt").write_text(
        "flag{93a3880a-5dfa-4bcb-8ee7-ea93bac9d214}", encoding="utf-8"
    )
    assert _pass_progress(report, 0, ws) is True
