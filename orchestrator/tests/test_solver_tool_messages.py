"""solver 消息历史卫生 —— 防止 tool_calls 组被 user 插话切断。

实测（run-34162 复盘的 pwn 场景）：一条 assistant 消息带 2 个 tool_calls 时，
「片快结束」提示被插在第一个 tool 结果后面，DeepSeek 直接 400：
`assistant message with 'tool_calls' must be followed by tool messages`。
历史一旦被污染，后续每次请求都 400，solver 只会空转到超时。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


@pytest.fixture
def common():
    sys.path.insert(0, str(SOLVER_DIR))
    sys.modules.pop("common", None)
    mod = importlib.import_module("common")
    yield mod
    sys.modules.pop("common", None)


def _call(cid: str) -> dict:
    return {
        "id": cid,
        "type": "function",
        "function": {"name": "run_command", "arguments": "{}"},
    }


def test_inserts_missing_tool_result_before_interleaved_user(common):
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [_call("a"), _call("b")]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "user", "content": "slice nearly over"},
        {"role": "tool", "tool_call_id": "b", "content": "late"},
    ]
    fixed = common.sanitize_tool_messages(messages)
    assert [m["role"] for m in fixed] == ["user", "assistant", "tool", "tool", "user"]
    assert fixed[2]["tool_call_id"] == "a"
    assert fixed[3]["tool_call_id"] == "b"  # 缺失结果补占位，保证组完整
    assert "unavailable" in fixed[3]["content"]
    assert fixed[4]["content"] == "slice nearly over"


def test_drops_orphan_tool_message(common):
    fixed = common.sanitize_tool_messages(
        [
            {"role": "user", "content": "go"},
            {"role": "tool", "tool_call_id": "ghost", "content": "x"},
        ]
    )
    assert fixed == [{"role": "user", "content": "go"}]


def test_valid_history_passes_through_unchanged(common):
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [_call("a")]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    assert common.sanitize_tool_messages(messages) == messages
