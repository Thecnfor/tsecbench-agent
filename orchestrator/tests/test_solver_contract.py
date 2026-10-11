"""solver/main.py 编排器模式（SOLVER_TASK）落盘契约测试。

真实 solver 的 LLM 循环无法离线跑，但「候选写 out/flags/NNN.txt +
result.json 收尾」这条编排器契约可以直接验证（§2.2）。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


@pytest.fixture
def solver_module(tmp_path, monkeypatch):
    """以编排器模式导入 solver.main（SOLVER_TASK 指向假 task.json）。"""
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"unique_code": "x1", "targets": ["10.0.0.1:80"]}), encoding="utf-8")
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.delenv("BOXPWNR_PROMPT", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    sys.modules.pop("main", None)
    sys.modules.pop("common", None)  # 契约状态真值源，须随 main 一同重载
    mod = importlib.import_module("main")
    yield mod, task
    sys.modules.pop("main", None)
    sys.modules.pop("common", None)


def test_orch_mode_detected(solver_module):
    mod, _ = solver_module
    assert mod.ORCH_MODE is True


def test_report_candidate_writes_flag_file(solver_module):
    mod, task = solver_module
    mod.report_candidate("FLAG{abc}")
    mod.report_candidate("FLAG{abc}")  # 去重
    mod.report_candidate("FLAG{def}")
    flags_dir = task.parent / "out" / "flags"
    files = sorted(p.name for p in flags_dir.iterdir())
    assert files == ["000.txt", "001.txt"]
    assert (flags_dir / "000.txt").read_text(encoding="utf-8").strip() == "FLAG{abc}"
    assert (flags_dir / "001.txt").read_text(encoding="utf-8").strip() == "FLAG{def}"


def test_write_result_summary(solver_module):
    mod, task = solver_module
    mod.report_candidate("FLAG{xyz}")
    mod.write_result("solved", evidence="found in /flag")
    result = json.loads((task.parent / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "solved"
    assert result["candidates"] == ["FLAG{xyz}"]
    assert result["evidence"] == "found in /flag"


def test_load_task_tolerates_missing(solver_module, monkeypatch):
    mod, _ = solver_module
    # 契约状态单一真值源在 common（main 经 __getattr__ 转发），patch 必须落在本体
    monkeypatch.setattr(mod.common, "SOLVER_TASK_PATH", "Z:/nonexistent/nope.json")
    assert mod.load_task() == {}


def test_no_api_key_exits_2(solver_module, monkeypatch, capsys):
    """编排器模式同样要求 LLM_API_KEY（无模型 = 零分，硬失败）。"""
    mod, _ = solver_module
    monkeypatch.setattr(mod, "API_KEY", None)
    assert mod.main() == 2
