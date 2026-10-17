"""prior 新键的合并与提示注入。"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from orchestrator.main import _merge_prior

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


def test_merge_prior_merges_credentials_and_findings():
    merged = _merge_prior(
        {"credentials": ["a:b"], "findings": ["nginx/1.18.0"]},
        {"credentials": ["a:b", "c:d"], "findings": ["tomcat/9.0.1"]},
    )
    assert merged["credentials"] == ["a:b", "c:d"]
    assert merged["findings"] == ["nginx/1.18.0", "tomcat/9.0.1"]


def test_setup_orch_mode_injects_new_sections(monkeypatch, tmp_path):
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps(
            {
                "description": "d",
                "targets": ["10.0.0.1:80"],
                "prior": {"credentials": ["admin:pass"], "findings": ["nginx/1.18.0"]},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.setenv("SOLVER_DEADLINE_EPOCH", "9999999999")
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    common = importlib.import_module("common")
    common.setup_orch_mode()
    assert "# Confirmed credentials" in common.TASK_PROMPT
    assert "admin:pass" in common.TASK_PROMPT
    assert "# Findings (versions/endpoints)" in common.TASK_PROMPT


def test_setup_orch_mode_injects_startup_knowledge(monkeypatch, tmp_path):
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps(
            {
                "description": "合同审批系统 接口 临时调整 权限 绕过 管理员",
                "targets": ["10.0.0.1:80"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.setenv("SOLVER_DEADLINE_EPOCH", "9999999999")
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    common = importlib.import_module("common")
    common.setup_orch_mode()
    assert "# KNOWLEDGE" in common.TASK_PROMPT


def test_setup_orch_mode_injects_fast_path(monkeypatch, tmp_path):
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps(
            {
                "description": "合同审批系统 接口 临时调整 管理员审批",
                "targets": ["10.0.0.1:80"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.setenv("SOLVER_DEADLINE_EPOCH", "9999999999")
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    common = importlib.import_module("common")
    common.setup_orch_mode()
    assert "# FAST PATH" in common.TASK_PROMPT


def test_setup_orch_mode_renders_relay_protocol(monkeypatch, tmp_path):
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps(
            {
                "description": "d",
                "targets": ["10.0.0.1:80"],
                "prior": {
                    "assets": ["10.0.0.1:80"],
                    "credentials": ["admin:pass"],
                    "history": (
                        "$ curl http://10.0.0.1/approve.php => exit_code=0 seconds=0.1"
                        " | 404 not found\n"
                        "$ curl http://10.0.0.1/dashboard.php => exit_code=0 seconds=0.1"
                        " | HTTP/1.1 200 OK"
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.setenv("SOLVER_DEADLINE_EPOCH", "9999999999")
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    common = importlib.import_module("common")
    common.setup_orch_mode()
    assert "RELAY PROTOCOL" in common.TASK_PROMPT
    assert "do NOT repeat" in common.TASK_PROMPT
    assert "approve.php" in common.TASK_PROMPT
    assert "admin:pass" in common.TASK_PROMPT
