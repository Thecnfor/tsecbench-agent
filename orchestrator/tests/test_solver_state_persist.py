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
            "content": '{"DATABASE_PASSWORD":"S3cretPwd!","DATABASE_USER":"dbadmin"}',
        },
    ]
    main._persist_harness_state(messages)
    prior = json.loads((tmp_path / "out" / "prior.json").read_text(encoding="utf-8"))
    assert any("curl -s http://10.0.0.1:8080" in item for item in prior["explored"])
    assert any("S3cretPwd!" in item for item in prior["credentials"])
    assert "credentials:" in (tmp_path / "out" / "findings.txt").read_text(encoding="utf-8")


def test_flag_nudge_only_when_new_flag_and_flags_remain(monkeypatch, tmp_path):
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"flag_count": 3, "correct_flag_count": 0}), encoding="utf-8")
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    import common

    common.TASK = {"flag_count": 3, "correct_flag_count": 0}
    monkeypatch.setattr(common, "_reported", {"FLAG{a}"})
    nudge = main._flag_nudge(common, 0)
    assert "FLAG{a}" not in (nudge or "")
    assert "2" in (nudge or "")
    assert main._flag_nudge(common, 1) is None


def test_fact_signature_detects_credentials_and_endpoints(monkeypatch, tmp_path):
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"unique_code": "x1"}), encoding="utf-8")
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    facts = main._fact_signature('{"DATABASE_PASSWORD":"S3cretPwd!"} http://10.0.0.1:8080/api')
    assert any(item.startswith("cred:") for item in facts)
    assert any(item.startswith("host:") for item in facts)
    assert main._fact_signature("just prose, no facts") == set()


def test_fact_signature_same_host_paths_are_not_new(monkeypatch, tmp_path):
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"unique_code": "x1"}), encoding="utf-8")
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    first = main._fact_signature("http://10.0.0.1:8080/a")
    second = main._fact_signature("http://10.0.0.1:8080/b")
    assert first == second
    assert main._fact_signature("admin:pass@10.0.0.1") != first


def test_fact_signature_ignores_public_and_static_hosts(monkeypatch, tmp_path):
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"unique_code": "x1"}), encoding="utf-8")
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    facts = main._fact_signature("http://fonts.googleapis.com/css http://10.0.0.1:8080/a")
    assert facts == {"host:10.0.0.1:8080"}
