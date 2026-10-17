"""Harness 工具层回归 —— 预算硬顶 + 结构化 recon 工具（不靠提示词约束模型）。"""

from __future__ import annotations

import importlib
import os
import sys
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


def test_tool_list_exposes_structured_tools(common):
    names = {t["function"]["name"] for t in common.build_tool_specs()}
    assert names == {"run_command", "recon", "rea", "kb"}


def test_rea_decompile_returns_code(common, monkeypatch, tmp_path):
    target = tmp_path / "bin"
    target.write_bytes(b"\x7fELF")
    fake = tmp_path / "rea"
    fake.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        '{"normalized_result":"undefined4 main(){ return 0; }",'
        '"provider":{"name":"Ghidra"},"parameters":{"procedure":"main"},'
        '"limitations":["l1"]}\n'
        "EOF\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    out = common.run_rea({"mode": "decompile", "path": str(target), "procedure": "main"})
    assert "main" in out["code"]
    assert out["provider"] == "Ghidra"
    assert out["limitations"] == ["l1"]


def test_rea_rejects_missing_file(common):
    assert "error" in common.run_rea({"mode": "inspect", "path": "/no/such/file"})


def test_command_timeout_is_hard_capped(common):
    assert common.effective_command_timeout(600, remaining=600) == common.CMD_HARD_MAX_S
    assert common.effective_command_timeout(60, remaining=None) == 60
    assert common.effective_command_timeout(60, remaining=40) == 20  # 半片约束
    assert common.effective_command_timeout(1, remaining=300) == 5


def test_recon_dispatch_host_and_web(common, monkeypatch):
    fake = types.SimpleNamespace(
        recon_host=lambda target: {"target": target, "open_ports": ["80"]},
        recon_web=lambda url: {"url": url, "dirs": ["/admin"]},
        derive_web_url=lambda target, ports: "http://x/",
    )
    monkeypatch.setitem(sys.modules, "recon_recipe", fake)
    assert common.run_recon({"mode": "host", "target": "10.0.0.1"})["host"]["open_ports"] == ["80"]
    assert common.run_recon({"mode": "web", "url": "http://x/"})["web"]["dirs"] == ["/admin"]
    both = common.run_recon({"mode": "all", "target": "10.0.0.1", "url": "http://x/"})
    assert {"host", "web"} <= set(both)


def test_recon_all_derives_url_from_open_ports(common, monkeypatch):
    fake = types.SimpleNamespace(
        recon_host=lambda target: {"target": target, "open_ports": ["8080"]},
        recon_web=lambda url: {"url": url, "dirs": []},
        derive_web_url=lambda target, ports: f"http://{target}:{ports[0]}",
    )
    monkeypatch.setitem(sys.modules, "recon_recipe", fake)
    out = common.run_recon({"mode": "all", "target": "10.0.0.1"})
    assert out["web"]["url"] == "http://10.0.0.1:8080"
    assert out["host"]["target"] == "10.0.0.1"


def test_recon_rejects_missing_params(common, monkeypatch):
    fake = types.SimpleNamespace(recon_host=None, recon_web=None)
    monkeypatch.setitem(sys.modules, "recon_recipe", fake)
    assert "error" in common.run_recon({"mode": "host"})
    assert "error" in common.run_recon({"mode": "web"})
    assert "error" in common.run_recon({"mode": "bogus"})


def test_summarize_history_preserves_commands_and_results(common):
    msgs = [
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
                        "arguments": '{"command":"curl -i http://t/"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "a",
            "content": "[exit_code=0 seconds=0.2]\nHTTP/1.1 200 OK\nbody",
        },
    ]
    text = common.summarize_history(msgs)
    assert "curl -i http://t/" in text
    assert "exit_code=0" in text
    assert "HTTP/1.1 200 OK" in text


def test_summarize_history_empty_without_tool_exchanges(common):
    assert common.summarize_history([{"role": "user", "content": "hi"}]) == ""


def test_system_prompt_is_concise_and_capability_first(common):
    prompt = common.build_system_prompt()
    assert len(prompt) < 2500, f"prompt too long: {len(prompt)} chars"
    for tool in ("run_command", "recon", "rea", "kb"):
        assert tool in prompt


def test_kb_tool_returns_snippets(common, monkeypatch):
    monkeypatch.setattr(common, "kb_snippets", lambda q, top_k=3: "PAYLOAD: ' OR 1=1--")
    out = common.run_kb({"query": "sql injection"})
    assert "PAYLOAD" in out["snippets"]


def test_prompt_contains_attack_loop(common):
    prompt = common.build_system_prompt()
    assert "# ATTACK LOOP" in prompt
    assert "reuse" in prompt.lower()
    assert "flag" in prompt.lower()
    assert len(prompt) < 2800


def test_generic_flag_rejects_css_and_js_shapes(common):
    for text in (
        "body{color:#fff;background:#000}",
        "h1{border-right:1px solid rgba(255,255,255,.3)}",
        "let{children:t}",
        "try{r=globalThis.localStorage,n=globalThis.sessionStorage}",
        "catch{r=new i,n=new i}",
    ):
        assert common.find_flags(text, include_generic=True) == [], text


def test_lowercase_flag_is_a_named_pattern(common):
    flag = "flag{ce38f9c5-2635-4c27-8209-ee21e32559f0}"
    assert common.find_flags(flag) == [flag]


def test_generic_accepts_flag_like_body(common):
    flag = "Flag{abc_def-123}"
    assert common.find_flags(flag, include_generic=True) == [flag]


def test_recon_attaches_kb_hits(common, monkeypatch):
    fake = types.SimpleNamespace(
        recon_host=lambda target: {"target": target, "open_ports": ["80"]},
        recon_web=lambda url: {"url": url, "dirs": []},
        derive_web_url=lambda target, ports: "http://x/",
    )
    monkeypatch.setitem(sys.modules, "recon_recipe", fake)
    monkeypatch.setattr(common, "kb_snippets", lambda query, top_k=3: "KBX")
    out = common.run_recon({"mode": "all", "target": "10.0.0.1"})
    assert out["kb"] == "KBX"


def test_plain_nudge_threshold_is_relaxed(common):
    assert common.MAX_PLAIN_NUDGES >= 6


def test_recon_attaches_fast_path(common, monkeypatch):
    fake = types.SimpleNamespace(
        recon_host=lambda target: {"target": target, "open_ports": ["3000"]},
        recon_web=lambda url: {"url": url, "title": "Dify", "tech": ["next.js"]},
        derive_web_url=lambda target, ports: "http://x:3000/",
    )
    monkeypatch.setitem(sys.modules, "recon_recipe", fake)
    monkeypatch.setattr(common, "kb_snippets", lambda query, top_k=3: "")
    monkeypatch.setattr(common, "_AUTO_PROBED", set())
    monkeypatch.setattr(
        common,
        "run_command",
        lambda cmd, timeout: {"output": "", "exit_code": 0, "seconds": 0.0, "timed_out": False},
    )
    out = common.run_recon({"mode": "web", "url": "http://x:3000/"})
    assert "# FAST PATH" in out["fast_path"]


def test_no_fact_guard_constants(common):
    assert common.NO_FACT_NUDGE_TURNS >= 8
    assert common.MAX_NO_FACT_NUDGES >= 2
    assert common.MAX_TURNS_NO_FLAG >= 30


def test_recon_auto_probes_first_get_step(common, monkeypatch):
    fake = types.SimpleNamespace(
        recon_host=lambda target: {"target": target, "open_ports": ["3000"]},
        recon_web=lambda url: {"url": url, "title": "Dify", "tech": ["next.js"]},
        derive_web_url=lambda target, ports: "http://x:3000/",
    )
    monkeypatch.setitem(sys.modules, "recon_recipe", fake)
    monkeypatch.setattr(common, "kb_snippets", lambda query, top_k=3: "")
    monkeypatch.setattr(common, "_AUTO_PROBED", set())
    monkeypatch.setattr(common, "TARGET_INFO", "10.0.0.1:3000")
    seen = {}

    def fake_run(cmd, timeout):
        seen["cmd"] = cmd
        return {
            "output": "HTTP/1.1 200 OK\n{}",
            "exit_code": 0,
            "seconds": 0.0,
            "timed_out": False,
        }

    monkeypatch.setattr(common, "run_command", fake_run)
    out = common.run_recon({"mode": "web", "url": "http://x:3000/"})
    assert "console/api/setup" in seen["cmd"]
    assert "fast_probe" in out


def test_placeholder_flags_are_rejected(common):
    assert common.find_flags("FLAG{...}") == []
    assert common.find_flags("FLAG{<flag>}") == []
    assert common.find_flags("FLAG{xxx}") == []
    real = "flag{ce38f9c5-2635-4c27-8209-ee21e32559f0}"
    assert common.find_flags(real) == [real]
