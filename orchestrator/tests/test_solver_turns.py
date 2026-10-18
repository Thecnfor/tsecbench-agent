"""轮数预算回归 —— 编排器模式必须以时间片为准，不能被 60 轮截断。

难题常常需要上百轮工具交互；`MAX_TURNS=60` 只是 BoxPwnr 直跑模式的保险丝。
编排器已经给了 SOLVER_DEADLINE_EPOCH 硬截止，再按轮数停会直接丢掉本可解出的题。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


def load_common(monkeypatch, tmp_path: Path, *, orch: bool):
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    if orch:
        task = tmp_path / "task.json"
        task.write_text(json.dumps({"unique_code": "x1"}), encoding="utf-8")
        monkeypatch.setenv("SOLVER_TASK", str(task))
        monkeypatch.setenv("SOLVER_DEADLINE_EPOCH", "9999999999")
    else:
        monkeypatch.delenv("SOLVER_TASK", raising=False)
        monkeypatch.delenv("BOXPWNR_PROMPT", raising=False)
        monkeypatch.delenv("SOLVER_DEADLINE_EPOCH", raising=False)
    sys.modules.pop("common", None)
    mod = importlib.import_module("common")
    return mod


def test_orchestrator_mode_is_time_bounded_not_turn_bounded(monkeypatch, tmp_path):
    common = load_common(monkeypatch, tmp_path, orch=True)
    assert common.ORCH_MODE is True
    assert common.turn_limit() >= 1_000_000


def test_standalone_mode_keeps_the_safety_cap(monkeypatch, tmp_path):
    common = load_common(monkeypatch, tmp_path, orch=False)
    assert common.ORCH_MODE is False
    assert common.turn_limit() == common.MAX_TURNS
