"""上下文预算回归 —— token 估算、裁剪窗口、溢出恢复与输出落盘。

实测 c-09 80 轮累计 1.56M prompt tokens，唯一一次裁剪由 `len(messages)>140`
触发；旧实现摘要窗口与保留尾部重叠，导致被丢弃的早期命令没有任何摘要。
本文件锁住三件事：裁剪确实摘要"被丢弃区间"、尾部按 token 预算保留且不破坏
tool_calls 组、上下文溢出 400 会先紧急压缩而不是空转到截止。
"""

from __future__ import annotations

import importlib
import json
import sys
import time
import types
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


def _turn(index: int, *, out_chars: int = 120) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": f"plan-{index} " + "p" * 80,
            "tool_calls": [
                {
                    "id": f"c{index}",
                    "type": "function",
                    "function": {
                        "name": "run_command",
                        "arguments": json.dumps({"command": f"cmd-{index}"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": f"c{index}",
            "content": f"[exit_code=0]\nmarker-{index} " + "o" * out_chars,
        },
    ]


def _history(turns: int, *, out_chars: int = 120) -> list[dict]:
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "go"},
    ]
    for index in range(turns):
        messages.extend(_turn(index, out_chars=out_chars))
    return messages


def test_estimate_tokens_treats_cjk_as_one_token(common):
    assert common.estimate_tokens("") == 0
    assert common.estimate_tokens("abcd") == 1
    assert common.estimate_tokens("你好") == 2
    assert common.estimate_tokens("ab你好") == 3


def test_compact_history_summarizes_the_dropped_range(common):
    messages = _history(120)
    compacted, dropped, before, after = common.compact_history(
        messages, target_tokens=2000, summary_limit=200
    )

    assert dropped > 0
    assert before > after
    assert compacted[0]["role"] == "system"
    assert compacted[2]["role"] == "user"
    assert "[context trimmed" in compacted[2]["content"]
    assert "cmd-0" in compacted[2]["content"]
    assert "HARNESS STATE" in compacted[2]["content"]
    # 早期命令只存在于摘要里，不能又出现在保留的工具结果中
    assert not any(
        msg.get("role") == "tool" and "marker-0 " in str(msg.get("content")) for msg in compacted
    )


def test_compact_history_keeps_tool_call_groups_intact(common):
    messages = _history(120)
    compacted, *_ = common.compact_history(messages, target_tokens=4000)

    assert compacted[0]["role"] == "system"
    assert compacted[0]["content"] == "system prompt"
    assert compacted[1]["role"] == "user"
    assert compacted[2]["role"] == "user"  # 摘要插在 head 之后
    assert compacted[3].get("role") != "tool"
    assert common.sanitize_tool_messages(compacted) == compacted


def test_compact_history_respects_the_token_target(common):
    messages = _history(120)
    compacted, *_ = common.compact_history(messages, target_tokens=3000)
    assert common.conversation_tokens(compacted) <= 3600


def test_compact_history_caps_a_single_oversized_message(common):
    messages = _history(20)
    messages.append(
        {
            "role": "tool",
            "tool_call_id": "huge",
            "content": "[exit_code=0]\n" + "z" * 50000,
        }
    )
    compacted, *_ = common.compact_history(messages, target_tokens=1500)
    assert common.conversation_tokens(compacted) <= 2500


def test_elide_stale_messages_keeps_recent_turns_verbatim(common):
    messages = _history(40, out_chars=600)
    elided, count = common.elide_stale_messages(messages, keep_recent_turns=5)

    assert count > 0
    assert [m["tool_call_id"] for m in elided if m["role"] == "tool"] == [
        m["tool_call_id"] for m in messages if m["role"] == "tool"
    ]
    old_tool = next(
        m for m in elided[2:-10] if m.get("role") == "tool" and "marker-0 " in str(m.get("content"))
    )
    assert len(old_tool["content"]) < 320
    assert "elided" in old_tool["content"]

    original_recent = [m for m in messages if m["role"] == "assistant"][-5:]
    compacted_recent = [m for m in elided if m["role"] == "assistant"][-5:]
    assert compacted_recent == original_recent


def test_elide_stale_messages_is_idempotent(common):
    messages = _history(40)
    once, _ = common.elide_stale_messages(messages, keep_recent_turns=5)
    twice, count = common.elide_stale_messages(once, keep_recent_turns=5)
    assert count == 0
    assert twice == once


def test_prepare_tool_output_spills_large_body_to_file(common, monkeypatch, tmp_path):
    monkeypatch.setattr(common, "SPILL_CHARS", 200)
    monkeypatch.setattr(common, "_spill_dir", lambda: tmp_path)
    body = "A" * 5000

    rendered = common.prepare_tool_output(body, label="turn-1")

    assert "output spilled" in rendered
    assert str(tmp_path) in rendered
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == body
    assert len(rendered) < common.SPILL_PREVIEW_CHARS + 400


def test_prepare_tool_output_condenses_html_preview(common, monkeypatch, tmp_path):
    monkeypatch.setattr(common, "SPILL_CHARS", 200)
    monkeypatch.setattr(common, "_spill_dir", lambda: tmp_path)
    raw = (
        "<html><head><style>.x{color:red}</style></head><body>"
        "<script>evil()</script><h1>Hello</h1><p>World</p></body></html>"
    ) * 20

    rendered = common.prepare_tool_output(raw, label="web")

    assert "<h1>" not in rendered
    assert "<script" not in rendered
    assert "Hello" in rendered
    assert "World" in rendered
    spilled = next(tmp_path.iterdir()).read_text(encoding="utf-8")
    assert "<h1>" in spilled  # 原始输出仍可 grep


def test_prepare_tool_output_respects_per_turn_budget(common, monkeypatch, tmp_path):
    monkeypatch.setattr(common, "SPILL_CHARS", 5000)
    monkeypatch.setattr(common, "_spill_dir", lambda: tmp_path)
    body = "B" * 4000

    rendered = common.prepare_tool_output(body, label="turn-2", turn_budget_left=300)

    assert "output spilled" in rendered
    assert len(rendered) < 500
    assert next(tmp_path.iterdir()).read_text(encoding="utf-8") == body


def test_context_overflow_detection(common):
    assert common.is_context_overflow_error(
        Exception("Error code: 400 - maximum context length is 131072 tokens")
    )
    assert common.is_context_overflow_error(Exception("context_length_exceeded"))
    assert common.is_context_overflow_error(Exception("input is too long"))
    assert not common.is_context_overflow_error(Exception("invalid api token"))
    assert not common.is_context_overflow_error(Exception("connection reset by peer"))


def test_emergency_compact_fits_a_hard_budget(common):
    compacted = common.emergency_compact(_history(120))
    assert compacted[0]["role"] == "system"
    assert common.conversation_tokens(compacted) <= common.EMERGENCY_TOKENS + 2500
    assert common.sanitize_tool_messages(compacted) == compacted


def test_render_prior_items_is_bounded_and_prefers_recent(common):
    items = [f"cmd-{index} " + "x" * 300 for index in range(40)]
    text = common.render_prior_items("Explored", items, limit=5)
    lines = text.splitlines()

    assert lines[0] == "# Explored"
    assert len(lines) == 7  # 标题 + 5 条 + omitted 提示
    assert "cmd-39" in text
    assert "cmd-0 " not in text
    assert "older entries omitted" in text
    assert all(len(line) <= 220 for line in lines)


def test_setup_orch_mode_bounds_prior_history_and_renders_bullets(common, monkeypatch, tmp_path):
    task = {
        "unique_code": "x",
        "description": "demo",
        "targets": ["10.0.0.1"],
        "prior": {
            "assets": [f"asset-{index}" for index in range(40)],
            "explored": [f"explored-{index}" for index in range(40)],
            "credentials": ["admin:secret"],
            "history": "XYZQ" * 2500,
        },
    }
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    monkeypatch.setattr(common, "SOLVER_TASK_PATH", str(task_path))
    monkeypatch.setattr(common, "TASK_PROMPT", "")

    common.setup_orch_mode()

    assert common.TASK_PROMPT.count("XYZQ") == 500
    assert "- asset-39" in common.TASK_PROMPT
    assert "asset-0\n" not in common.TASK_PROMPT
    assert "older entries omitted" in common.TASK_PROMPT
    assert '"asset-' not in common.TASK_PROMPT


def test_context_log_appends_one_json_line(common, monkeypatch, tmp_path):
    task_path = tmp_path / "task.json"
    task_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(common, "SOLVER_TASK_PATH", str(task_path))

    common.log_context_event({"turn": 1, "prompt_tokens": 10})

    lines = (tmp_path / "out" / "context.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"turn": 1, "prompt_tokens": 10}]


def test_main_recovers_from_context_overflow_instead_of_spinning(common, monkeypatch, tmp_path):
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps({"unique_code": "x1"}), encoding="utf-8")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("SOLVER_MAX_PLAIN_NUDGES", "1")
    monkeypatch.setenv("SOLVER_TASK", str(task_path))
    monkeypatch.setenv("SOLVER_DEADLINE_EPOCH", "9999999999")
    monkeypatch.delenv("BOXPWNR_PROMPT", raising=False)
    sys.modules.pop("common", None)
    common = importlib.import_module("common")
    assert common.API_KEY == "test-key"
    assert common.ORCH_MODE is True

    calls: list[int] = []

    class FakeCompletions:
        def create(self, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("maximum context length is 131072 tokens")
            message = types.SimpleNamespace(content="done", tool_calls=None)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = FakeOpenAI
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    sys.modules.pop("main", None)
    solver_main = importlib.import_module("main")

    started = time.monotonic()
    assert solver_main.main() == 0
    assert len(calls) >= 2  # 第一次溢出后压缩重试，第二次成功
    assert time.monotonic() - started < 4  # 溢出路径不 sleep 5s

    events = [
        json.loads(line)
        for line in (tmp_path / "out" / "context.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event.get("compaction") == "emergency" for event in events)


def test_capped_tool_arguments_remain_valid_json(common):
    huge = json.dumps({"command": "x" * 200000})
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "run_command", "arguments": huge},
            }
        ],
    }
    capped = common._cap_message(message, 1000)
    parsed = json.loads(capped["tool_calls"][0]["function"]["arguments"])
    assert parsed.get("_elided") is True
