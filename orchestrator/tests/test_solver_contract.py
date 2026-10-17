"""solver/main.py 编排器模式（SOLVER_TASK）落盘契约测试。

真实 solver 的 LLM 循环无法离线跑，但「候选写 out/flags/NNN.txt +
result.json 收尾」这条编排器契约可以直接验证（§2.2）。
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeCall:
    def __init__(self, call_id: str, name: str, arguments: str):
        self.id = call_id
        self.function = _FakeFunction(name, arguments)

    def model_dump(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.function.name, "arguments": self.function.arguments},
        }


def _response(content: str | None = None, calls: list | None = None):
    message = types.SimpleNamespace(content=content, tool_calls=calls or [])
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=None)


class _FakeEndpoint:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.requests: list[list[dict]] = []

    def create(self, **kwargs):
        import copy

        self.requests.append(copy.deepcopy(kwargs["messages"]))
        return self.responses.pop(0)


class _FakeClient:
    def __init__(self, endpoint: _FakeEndpoint):
        self.chat = types.SimpleNamespace(completions=endpoint)


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


def test_write_result_includes_tool_usage_and_next_idea(solver_module):
    mod, task = solver_module
    mod.common.TOOL_USAGE.clear()
    mod.common.note_tool_use("run_command")
    mod.common.note_tool_use("run_command")
    mod.common.note_tool_use("kb")
    mod.write_result("solved", evidence="e", next_idea="pivot to 8080")
    result = json.loads((task.parent / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["tools_used"] == [
        {"name": "kb", "calls": 1},
        {"name": "run_command", "calls": 2},
    ]
    assert result["next_idea"] == "pivot to 8080"


def test_main_runs_structured_tool_calls_concurrently(solver_module, monkeypatch):
    """一轮内多个 recon/rea/kb 调用必须并发执行，而不是排队。"""
    mod, _task = solver_module
    monkeypatch.setattr(mod, "API_KEY", "sk-test")
    threads: list[str] = []

    def slow_kb(args):
        import threading
        import time as _time

        threads.append(threading.current_thread().name)
        _time.sleep(0.2)
        return {"query": args["query"]}

    monkeypatch.setattr(mod.common, "run_kb", slow_kb)
    endpoint = _FakeEndpoint(
        [
            _response(
                calls=[
                    _FakeCall("k1", "kb", '{"query": "a"}'),
                    _FakeCall("k2", "kb", '{"query": "b"}'),
                ]
            ),
            _response(content="FLAG{abc}"),
        ]
    )
    fake_openai = types.SimpleNamespace(OpenAI=lambda **kwargs: _FakeClient(endpoint))
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    assert mod.main() == 0
    assert len(threads) == 2
    assert len(set(threads)) == 2


def test_load_task_tolerates_missing(solver_module, monkeypatch):
    mod, _ = solver_module
    # 契约状态单一真值源在 common（main 经 __getattr__ 转发），patch 必须落在本体
    monkeypatch.setattr(mod.common, "SOLVER_TASK_PATH", "Z:/nonexistent/nope.json")
    assert mod.load_task() == {}


def test_main_rejects_unknown_tool_and_non_object_arguments(solver_module, monkeypatch):
    """畸形工具调用必须变成 tool error，而不是让 solver 崩溃。"""
    mod, task = solver_module
    monkeypatch.setattr(mod, "API_KEY", "sk-test")
    endpoint = _FakeEndpoint(
        [
            _response(calls=[_FakeCall("c1", "bash", '{"command":"id"}')]),
            _response(calls=[_FakeCall("c2", "run_command", "[]")]),
            _response(content="FLAG{abc}"),
        ]
    )
    fake_openai = types.SimpleNamespace(OpenAI=lambda **kwargs: _FakeClient(endpoint))
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    assert mod.main() == 0
    tool_messages = [m.get("content") for m in endpoint.requests[-1] if m.get("role") == "tool"]
    assert any("unknown tool" in str(content) for content in tool_messages)
    assert any("JSON object" in str(content) for content in tool_messages)
    assert (task.parent / "out" / "flags" / "000.txt").read_text(encoding="utf-8").strip() == (
        "FLAG{abc}"
    )


def test_no_api_key_exits_2(solver_module, monkeypatch, capsys):
    """编排器模式同样要求 LLM_API_KEY（无模型 = 零分，硬失败）。"""
    mod, _ = solver_module
    monkeypatch.setattr(mod, "API_KEY", None)
    assert mod.main() == 2


def test_orch_mode_injects_prior_history(tmp_path, monkeypatch):
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps({"unique_code": "x1", "prior": {"history": "$ curl http://t/ => 200"}}),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.delenv("BOXPWNR_PROMPT", raising=False)
    sys.modules.pop("main", None)
    sys.modules.pop("common", None)
    mod = importlib.import_module("main")
    try:
        mod.setup_orch_mode()
        assert "Previous attempt" in mod.TASK_PROMPT
        assert "$ curl http://t/" in mod.TASK_PROMPT
    finally:
        sys.modules.pop("main", None)
        sys.modules.pop("common", None)
