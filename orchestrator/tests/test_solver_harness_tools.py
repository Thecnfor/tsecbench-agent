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
    mod.reset_shell_session()
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
        lambda cmd, timeout, isolated=False: {
            "output": "",
            "exit_code": 0,
            "seconds": 0.0,
            "timed_out": False,
        },
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

    def fake_run(cmd, timeout, isolated=False):
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


def test_persistent_shell_keeps_vars_and_cwd(common, tmp_path):
    common.reset_shell_session()
    assert common.run_command("X=hello", 5)["exit_code"] == 0
    assert "hello" in common.run_command("echo $X", 5)["output"]
    common.run_command(f"cd {tmp_path}", 5)
    assert str(tmp_path) in common.run_command("pwd", 5)["output"]
    common.reset_shell_session()


def test_persistent_shell_timeout_restarts(common):
    common.reset_shell_session()
    out = common.run_command("sleep 30", 1)
    assert out["timed_out"] is True
    assert "ok" in common.run_command("echo ok", 5)["output"]
    common.reset_shell_session()


def test_persistent_shell_does_not_swallow_next_command(common):
    """交互式命令读 stdin 时不能吞掉下一条命令或伪造哨兵。"""
    common.reset_shell_session()
    first = common.run_command("cat", 2)
    assert first["timed_out"] is False
    assert first["exit_code"] == 0
    second = common.run_command("echo after-cat", 3)
    assert second["output"].strip() == "after-cat"
    assert "printf" not in second["output"]
    common.reset_shell_session()


def test_persistent_shell_heredoc_still_works(common):
    common.reset_shell_session()
    out = common.run_command("python3 - <<'PY'\nprint('heredoc-ok')\nPY", 5)
    assert "heredoc-ok" in out["output"]
    common.reset_shell_session()


def test_prompt_matches_persistent_shell_mode(common):
    prompt = common.build_system_prompt()
    if common.PERSISTENT_SHELL and not common.CONTAINER:
        assert "persist" in prompt.lower()
        assert "FRESH shell" not in prompt
    else:
        assert "FRESH shell" in prompt
    assert "stdin" in prompt.lower()


def test_parse_tool_arguments_rejects_non_object(common):
    args, error = common.parse_tool_arguments("[]")
    assert args is None
    assert "JSON object" in error
    args, error = common.parse_tool_arguments("null")
    assert args is None
    assert error


def test_parse_tool_arguments_accepts_object_and_reports_bad_json(common):
    args, error = common.parse_tool_arguments('{"command": "id"}')
    assert args == {"command": "id"}
    assert error is None
    args, error = common.parse_tool_arguments("{not json")
    assert args is None
    assert "invalid tool arguments" in error


def test_fast_probe_respects_remaining_slice(common, monkeypatch):
    monkeypatch.setattr(common, "TASK", {"description": "gradio app"})
    monkeypatch.setattr(common, "TARGET_INFO", "10.0.0.1:7860")
    monkeypatch.setattr(common, "_AUTO_PROBED", set())
    monkeypatch.setattr(common, "kb_snippets", lambda query, top_k=3: "")
    monkeypatch.setattr(common, "remaining_seconds", lambda: 9.0)
    seen = []

    def fake_run(cmd, timeout, isolated=False):
        seen.append((timeout, isolated))
        return {"output": "ok", "exit_code": 0, "seconds": 0.0, "timed_out": False}

    monkeypatch.setattr(common, "run_command", fake_run)
    fake = types.SimpleNamespace(recon_web=lambda url: {"url": url})
    monkeypatch.setitem(sys.modules, "recon_recipe", fake)
    common.run_recon({"mode": "web", "url": "http://10.0.0.1:7860/"})
    assert seen, "fast probes should run while slice time remains"
    assert all(isolated for _timeout, isolated in seen)
    assert all(timeout <= 9 for timeout, _isolated in seen)


def test_fast_probe_skipped_when_slice_almost_over(common, monkeypatch):
    monkeypatch.setattr(common, "TASK", {"description": "gradio app"})
    monkeypatch.setattr(common, "TARGET_INFO", "10.0.0.1:7860")
    monkeypatch.setattr(common, "_AUTO_PROBED", set())
    monkeypatch.setattr(common, "kb_snippets", lambda query, top_k=3: "")
    monkeypatch.setattr(common, "remaining_seconds", lambda: 4.0)
    seen = []
    monkeypatch.setattr(
        common, "run_command", lambda cmd, timeout, isolated=False: seen.append(cmd)
    )
    fake = types.SimpleNamespace(recon_web=lambda url: {"url": url})
    monkeypatch.setitem(sys.modules, "recon_recipe", fake)
    common.run_recon({"mode": "web", "url": "http://10.0.0.1:7860/"})
    assert seen == []


def test_rea_timeout_respects_remaining_slice(common, monkeypatch, tmp_path):
    target = tmp_path / "bin"
    target.write_bytes(b"\x7fELF")
    captured = {}

    class _Proc:
        stdout = '{"normalized_result": "main(){}"}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _Proc()

    monkeypatch.setattr(common.subprocess, "run", fake_run)
    monkeypatch.setattr(common, "remaining_seconds", lambda: 8.0)
    common.run_rea({"mode": "inspect", "path": str(target)})
    assert captured["timeout"] <= 5


def test_structured_tool_batch_runs_concurrently_in_order(common, monkeypatch):
    import time as _time

    def slow_kb(args):
        _time.sleep(0.3)
        return {"query": args["query"]}

    monkeypatch.setattr(common, "run_kb", slow_kb)
    started = _time.time()
    results = common.run_structured_batch(
        [
            {"name": "kb", "args": {"query": "a"}},
            {"name": "kb", "args": {"query": "b"}},
        ]
    )
    elapsed = _time.time() - started
    assert [result["query"] for result in results] == ["a", "b"]
    assert elapsed < 0.55


def test_structured_tool_batch_single_call_is_inline(common, monkeypatch):
    monkeypatch.setattr(common, "run_kb", lambda args: {"query": args["query"]})
    results = common.run_structured_batch([{"name": "kb", "args": {"query": "solo"}}])
    assert results == [{"query": "solo"}]


def test_structured_tool_descriptions_teach_when_to_use(common):
    specs = {
        spec["function"]["name"]: spec["function"]["description"]
        for spec in common.build_tool_specs()
    }
    assert "first" in specs["recon"].lower()
    assert "stuck" in specs["kb"].lower() or "before" in specs["kb"].lower()


def test_host_recon_auto_fingerprints_http_and_lists_known_issues(common, monkeypatch):
    fake = types.SimpleNamespace(
        recon_host=lambda target: {"target": target, "open_ports": ["3000"]},
        recon_web=lambda url: {"url": url, "title": "Dify", "tech": ["next.js"]},
        derive_web_url=lambda target, ports: "http://x:3000/",
    )
    monkeypatch.setitem(sys.modules, "recon_recipe", fake)
    monkeypatch.setattr(
        common,
        "kb_snippets",
        lambda query, top_k=3: "## KB: Dify console api\npayload\n## KB: next.js RSC\npayload",
    )
    monkeypatch.setattr(common, "_AUTO_PROBED", set())
    monkeypatch.setattr(
        common,
        "run_command",
        lambda cmd, timeout: {"output": "", "exit_code": 0, "seconds": 0.0, "timed_out": False},
    )
    out = common.run_recon({"mode": "host", "target": "x"})
    assert out["web"]["title"] == "Dify"
    assert any("Dify" in item for item in out["known_issues"])


def test_detect_known_terms_and_known_issue_snippet(common, monkeypatch):
    terms = common.detect_known_terms("Dify 1.2.0 is vulnerable to CVE-2025-3248")
    assert "dify" in terms and "CVE-2025-3248" in terms
    monkeypatch.setattr(
        common,
        "kb_snippets",
        lambda query, top_k=3: "## KB: CVE-2025-3248 payload\ncurl -s /api/v1/validate/code",
    )
    assert "CVE-2025-3248" in (common.known_issue_snippet("CVE-2025-3248") or "")
    monkeypatch.setattr(common, "kb_snippets", lambda query, top_k=3: "unrelated")
    assert common.known_issue_snippet("dify") is None


def test_command_failed_and_hypothesis_key_removed_after_merge():
    """1234 helpers were dropped by the upgrade-ab merge; see test suite history."""
    pass
