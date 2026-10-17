"""跨轮状态落盘：prior.json + findings.txt 必须携带凭据/命令。"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


def test_persist_writes_prior_and_findings(monkeypatch, tmp_path):
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps({"unique_code": "x1", "targets": ["10.0.0.1:8080"]}), encoding="utf-8"
    )
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.setenv("SOLVER_DEADLINE_EPOCH", "9999999999")
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")

    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "a",
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "arguments": json.dumps(
                            {"command": "curl -s http://10.0.0.1:8080/api/functions/order/config"}
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "a",
            "content": '{"DATABASE_PASSWORD":"flag{pwd_test}","DATABASE_USER":"dbadmin"}',
        },
    ]
    main._persist_harness_state(messages)
    prior = json.loads((tmp_path / "out" / "prior.json").read_text(encoding="utf-8"))
    assert any("curl -s http://10.0.0.1:8080" in item for item in prior["explored"])
    assert any("flag{pwd_test}" in item for item in prior["credentials"])
    assert "credentials:" in (tmp_path / "out" / "findings.txt").read_text(encoding="utf-8")
