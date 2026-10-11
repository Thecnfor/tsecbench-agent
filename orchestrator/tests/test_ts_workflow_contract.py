"""ts-workflow（Mastra 手动工作流 solver）编排器契约测试。

前提：`make build-ts-workflow` 已产出 dist/index.js（CI/本地没跑过构建则 skip，
与 mcp 的 npm 构建同策略）。用 SOLVER_PI_CMD 假 stub 注入 flag，离线验证
「out/flags/NNN.txt + result.json + session.jsonl + stdout <FLAG>」全链路。
task.json 不带 targets → recon 步零外部命令，纯离线。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DIST = REPO / "solver" / "ts-workflow" / "dist" / "index.js"

pytestmark = pytest.mark.skipif(
    not DIST.is_file(), reason="ts-workflow 未构建（make build-ts-workflow）"
)

STUB = """\
import sys
print("FLAG{ts_stub_1234}")
"""


def test_ts_workflow_contract(tmp_path, monkeypatch, capsys):
    task = tmp_path / "task.json"
    task.write_text(json.dumps({"unique_code": "ts1"}), encoding="utf-8")  # 无 targets → recon 跳过
    stub = tmp_path / "fake_pi.py"
    stub.write_text(STUB, encoding="utf-8")
    env = {
        **os.environ,
        "SOLVER_TASK": str(task),
        "SOLVER_PI_CMD": f"{sys.executable} {stub}",
        "LLM_BASE_URL": "https://gw.example.com/v1",
        "LLM_MODEL": "deepseek-chat",
    }
    env.pop("LLM_API_KEY", None)  # plan 步跳过（离线）
    env.pop("DEEPSEEK_API_KEY", None)
    proc = subprocess.run(
        ["node", str(DIST)], env=env, capture_output=True, text=True, timeout=120, cwd=tmp_path
    )
    assert proc.returncode == 0, proc.stderr[-2000:]

    flags_dir = task.parent / "out" / "flags"
    files = sorted(p.name for p in flags_dir.iterdir())
    assert files == ["000.txt"]
    assert (flags_dir / "000.txt").read_text(encoding="utf-8").strip() == "FLAG{ts_stub_1234}"

    result = json.loads((task.parent / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == "solved"
    assert result["candidates"] == ["FLAG{ts_stub_1234}"]

    events = [
        json.loads(line)
        for line in (task.parent / "out" / "session.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    kinds = [e["kind"] for e in events]
    assert "meta" in kinds and "profile" in kinds and "candidate" in kinds and "done" in kinds

    # flag 同时进 stdout（harvester 收割通道，§2.2）
    assert "<FLAG>" in proc.stdout or "FLAG{ts_stub_1234}" in proc.stdout
