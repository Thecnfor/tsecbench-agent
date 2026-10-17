"""dual_main.py（pi 执行内核适配器）编排器契约测试。

离线走通全链路：SOLVER_PI_CMD 注入假 pi stub（输出含 flag 的文本），
验证「候选写 out/flags/NNN.txt + result.json 收尾 + exit 0」。
超时 kill 路径与 pi_main.py 相同（killpg 整进程组），budget 下限 60s
不适宜在单测内真跑，由 run_bench 实测覆盖。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"

STUB = """\
import sys
print("recon done, captured: " + sys.argv[1][:0] + "FLAG{stub_1234}")
print("FLAG{stub_5678}")
"""


@pytest.fixture
def dual_module(tmp_path, monkeypatch):
    """以编排器模式导入 solver.dual_main，pi 替换为假 stub。"""
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"unique_code": "x1", "targets": ["10.0.0.1:80"]}), encoding="utf-8")
    monkeypatch.setenv("SOLVER_TASK", str(task))
    monkeypatch.delenv("BOXPWNR_PROMPT", raising=False)
    monkeypatch.delenv("SOLVER_PI_CMD", raising=False)
    stub = tmp_path / "fake_pi.py"
    stub.write_text(STUB, encoding="utf-8")
    monkeypatch.setenv("SOLVER_PI_CMD", f"{sys.executable} {stub}")
    sys.modules.pop("dual_main", None)
    sys.modules.pop("common", None)
    mod = importlib.import_module("dual_main")
    yield mod, task
    sys.modules.pop("dual_main", None)
    sys.modules.pop("common", None)


def test_stub_run_writes_flags_and_result(dual_module, capsys):
    mod, task = dual_module
    assert mod.main() == 0
    flags_dir = task.parent / "out" / "flags"
    files = sorted(p.name for p in flags_dir.iterdir())
    assert files == ["000.txt", "001.txt"]
    assert (flags_dir / "000.txt").read_text(encoding="utf-8").strip() == "FLAG{stub_1234}"
    result = json.loads((task.parent / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "solved"
    assert result["candidates"] == ["FLAG{stub_1234}", "FLAG{stub_5678}"]
    # 治理层产物：session 事件流 + prior 合成（Phase 2）
    session_lines = [
        json.loads(line)
        for line in (task.parent / "out" / "session.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [e["kind"] for e in session_lines]
    assert "profile" in kinds and "done" in kinds
    assert (task.parent / "out" / "prior.json").exists()
    # flag 同时进 stdout（harvester 的收割通道，§2.2）
    captured = capsys.readouterr().out
    assert "<FLAG>FLAG{stub_1234}</FLAG>" in captured


def test_build_pi_cmd_override(dual_module):
    mod, _ = dual_module
    cmd = mod.build_pi_cmd("solve it")
    assert cmd[-1] == "solve it"
    assert cmd[0] == sys.executable  # 假 stub 整条替换 npx 命令


def test_build_pi_cmd_default_npx(dual_module, monkeypatch):
    mod, _ = dual_module
    monkeypatch.delenv("SOLVER_PI_CMD", raising=False)
    monkeypatch.delenv("PI_BIN", raising=False)  # 本地 .env 可能设置 vendored pi
    monkeypatch.setenv("PI_PACKAGE", "@earendil-works/pi-coding-agent")
    monkeypatch.setenv("PI_VERSION", "1.1.0")
    cmd = mod.build_pi_cmd("solve it")
    assert cmd[0] == "npx"
    assert f"{mod.PI_PACKAGE}@{mod.PI_VERSION}" in cmd
    # Phase 4：启用 skills（agentDir/skills 渐进披露），仍禁 MCP
    assert "--no-skills" not in cmd and "--no-mcp" in cmd


def test_build_pi_cmd_pi_bin_precedes_npx(dual_module, monkeypatch):
    """镜像内 vendored pi（PI_BIN）优先于 npx —— 运行期离线（Phase 5）。"""
    mod, _ = dual_module
    monkeypatch.delenv("SOLVER_PI_CMD", raising=False)
    monkeypatch.setenv("PI_BIN", "/opt/tsecbench/pi/node_modules/.bin/pi")
    cmd = mod.build_pi_cmd("solve it")
    assert cmd[0] == "/opt/tsecbench/pi/node_modules/.bin/pi"
    assert "npx" not in cmd


def test_agent_dir_contents(dual_module, tmp_path, monkeypatch):
    mod, _ = dual_module
    agent_dir = tmp_path / "agent"
    monkeypatch.setenv("LLM_BASE_URL", "https://gw.example.com")
    monkeypatch.setattr(mod.common, "TASK_PROMPT", "# CONTRACT\n- solve")
    mod.build_agent_dir(agent_dir)
    models = json.loads((agent_dir / "models.json").read_text(encoding="utf-8"))
    provider = models["providers"]["deepseek"]
    assert provider["baseUrl"].endswith("/v1")
    assert provider["apiKey"] == "$LLM_API_KEY"
    append_system = (agent_dir / "APPEND_SYSTEM.md").read_text(encoding="utf-8")
    assert "CONTRACT" in append_system
    assert "recon" in append_system  # Phase 3 工具提示注入


def test_install_extensions_bakes_recipe_path(dual_module, tmp_path):
    mod, _ = dual_module
    agent_dir = tmp_path / "agent"
    mod.install_extensions(agent_dir)
    src = (agent_dir / "extensions" / "recon.ts").read_text(encoding="utf-8")
    assert "__RECON_RECIPE__" not in src
    assert str(mod.HERE / "recon_recipe.py") in src


def test_install_skills_from_kb(dual_module, tmp_path):
    mod, _ = dual_module
    agent_dir = tmp_path / "agent"
    mod.install_skills(agent_dir)
    skills = sorted(p.name for p in (agent_dir / "skills").iterdir())
    assert "web_recon" in skills
    skill = (agent_dir / "skills" / "web_recon" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: web_recon\ndescription:")
    assert "## " in skill  # 渐进披露：正文保留小节结构


def test_kb_snippets_injects_hits(dual_module):
    mod, _ = dual_module
    out = mod.kb_snippets("nginx server banner werkzeug debugger console CVE-2021-41773 apache")
    assert out.startswith("# KNOWLEDGE")
    assert "## KB:" in out


def test_kb_snippets_empty_query_returns_empty(dual_module):
    mod, _ = dual_module
    assert mod.kb_snippets("") == ""


def test_recon_recipe_bad_url_returns_json(dual_module):
    """工具缺失/输入错误都要出结构化 JSON，绝不崩（§1.6 预聚合契约）。"""
    mod, _ = dual_module
    import subprocess as sp
    import sys as _sys

    proc = sp.run(
        [_sys.executable, str(mod.HERE / "recon_recipe.py"), "web", "--url", "not-a-url"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0
    report = json.loads(proc.stdout)
    assert "input-error" in report["web"]["raw"]


def test_no_solver_task_exits_2(dual_module, monkeypatch):
    mod, _ = dual_module
    monkeypatch.delenv("SOLVER_TASK", raising=False)
    assert mod.main() == 2
