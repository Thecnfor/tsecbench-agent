"""Supervisor 单测 —— workspace 契约 + spawn/超时杀/白名单环境（§5.2/§7.1 #21）。"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from orchestrator.registry import ChallengeRecord
from orchestrator.supervisor import (
    collect_prior,
    prepare_workspace,
    run_solver,
)

PY = sys.executable


def rec(code: str = "t1", **kw) -> ChallengeRecord:
    kw.setdefault("unique_code", code)
    kw.setdefault("container_addr", ["10.0.0.1:8080"])
    return ChallengeRecord(**kw)


# ---- workspace / task.json 契约 ----
def test_workspace_writes_task_json(tmp_path):
    r = rec(flag_count=2, correct_flag_count=1, description="d")
    ws = prepare_workspace(tmp_path, r, attempt=2, prior={"assets": [{"v": 1}]})
    task = json.loads((ws / "task.json").read_text(encoding="utf-8"))
    assert task["unique_code"] == "t1"
    assert task["targets"] == ["10.0.0.1:8080"]  # 数组，不只第一个
    assert task["flag_count"] == 2 and task["correct_flag_count"] == 1
    assert task["attempt"] == 2
    assert "hint" not in task  # hint 链路已整体删除，task.json 不再有该字段
    assert task["prior"] == {"assets": [{"v": 1}]}
    assert (ws / "out" / "flags").is_dir()


def test_collect_prior_tolerates_missing_and_garbage(tmp_path):
    ws = prepare_workspace(tmp_path, rec(), attempt=1)
    assert collect_prior(ws) == {"assets": [], "explored": [], "open_leads": []}
    (ws / "out" / "prior.json").write_text("not json", encoding="utf-8")
    assert collect_prior(ws)["assets"] == []
    (ws / "out" / "prior.json").write_text('{"assets": [1,2], "bogus": 3}', encoding="utf-8")
    prior = collect_prior(ws)
    assert prior["assets"] == [1, 2] and "bogus" not in prior


# ---- spawn ----
def test_run_solver_success_writes_env_whitelist(tmp_path):
    """假 solver：校验环境白名单 + 读 task.json + 写 out/flags。"""
    script = tmp_path / "fake_solver.py"
    script.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "ws = Path(os.environ['SOLVER_TASK']).parent\n"
        "env = os.environ.copy()\n"
        "assert 'BENCHMARK_TOKEN' not in env, 'token leaked into solver env'\n"
        "assert 'LLM_API_KEY' in env\n"
        "assert 'PATH' in env\n"
        "task = json.loads(Path(os.environ['SOLVER_TASK']).read_text())\n"
        "assert task['targets'] == ['10.0.0.1:8080']\n"
        "print('candidate FLAG{from_stdout}', flush=True)\n"
        "(ws / 'out' / 'flags' / '000.txt').write_text('FLAG{from_file}\\n')\n"
        "(ws / 'out' / 'result.json').write_text('{\"status\": \"solved\"}')\n"
        "print('SOLVER_DEADLINE_EPOCH=' + os.environ['SOLVER_DEADLINE_EPOCH'])\n"
    )
    ws = prepare_workspace(tmp_path, rec(), attempt=1)
    outcome = asyncio.run(
        run_solver(
            solver_cmd=[PY, str(script)],
            ws=ws,
            deadline_epoch=time.time() + 60,
            slice_s=30,
            environ={
                "BENCHMARK_TOKEN": "MUST-NOT-LEAK",
                "LLM_API_KEY": "sk-x",
                "PATH": str(Path(PY).parent),
            },
        )
    )
    assert outcome.exit_code == 0 and not outcome.timed_out
    assert (ws / "out" / "flags" / "000.txt").read_text().strip() == "FLAG{from_file}"
    assert (ws / "stdout.log").read_text(encoding="utf-8").find("FLAG{from_stdout}") >= 0
    assert (ws / "out" / "result.json").is_file()


def test_run_solver_timeout_kills(tmp_path):
    """超时：SIGTERM→SIGKILL 杀进程组，exit 124；被杀前已写文件仍在（收割可救）。"""
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import time\n"
        "from pathlib import Path\n"
        "import os\n"
        "ws = Path(os.environ['SOLVER_TASK']).parent\n"
        "(ws / 'out' / 'flags' / '000.txt').write_text('FLAG{before_kill}\\n')\n"
        "print('wrote flag, sleeping forever', flush=True)\n"
        "time.sleep(999)\n"
    )
    ws = prepare_workspace(tmp_path, rec(), attempt=1)
    t0 = time.monotonic()
    outcome = asyncio.run(
        run_solver(
            solver_cmd=[PY, str(script)],
            ws=ws,
            deadline_epoch=time.time() + 60,
            slice_s=0.5,
            environ={"PATH": str(Path(PY).parent)},
        )
    )
    assert outcome.timed_out and outcome.exit_code == 124
    assert time.monotonic() - t0 < 20  # 杀掉而非等 999s
    assert (ws / "out" / "flags" / "000.txt").read_text().strip() == "FLAG{before_kill}"


def test_run_solver_crash_exit_code(tmp_path):
    script = tmp_path / "crash.py"
    script.write_text("import sys; print('boom'); sys.exit(3)\n")
    ws = prepare_workspace(tmp_path, rec(), attempt=1)
    outcome = asyncio.run(
        run_solver(
            solver_cmd=[PY, str(script)],
            ws=ws,
            deadline_epoch=time.time() + 60,
            slice_s=30,
            environ={"PATH": str(Path(PY).parent)},
        )
    )
    assert outcome.exit_code == 3 and not outcome.timed_out


def test_run_solver_missing_cmd(tmp_path):
    ws = prepare_workspace(tmp_path, rec(), attempt=1)
    outcome = asyncio.run(
        run_solver(
            solver_cmd=["definitely-not-a-real-binary-xyz"],
            ws=ws,
            deadline_epoch=time.time() + 60,
            slice_s=5,
            environ={"PATH": ""},
        )
    )
    assert outcome.exit_code == 127


@pytest.mark.parametrize("platform_guard", [sys.platform])
def test_windows_note(platform_guard):
    """文档性断言：Windows 上退化为杀单进程（无 killpg）。"""
    import os

    if platform_guard == "win32":
        assert not hasattr(os, "killpg") or os.name != "posix"
