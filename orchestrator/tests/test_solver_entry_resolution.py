"""solver 入口解析回归 —— 镜像 site-packages 布局（run-34162 根因）。

镜像里 orchestrator 被 pip install 到 site-packages，`Path(__file__).parents[3]`
指向 `venv/lib` 而不是仓库根，拼出的 `/opt/tsecbench/venv/lib/solver/main.py`
不存在。编排器当时只记到 solver_done exit_code=2，继续关靶场重投，整轮零提交。

解析顺序必须是：SOLVER_CMD（显式覆盖）→ TSECBENCH_SOLVER_DIR → 仓库相对路径；
任何候选都不存在时**启动即硬失败**，不能静默烧完整轮。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from orchestrator.config import RuntimeConfig
from orchestrator.main import resolve_solver_cmd

from orchestrator import main as main_mod


def cfg(solver_cmd: list[str] | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        mode="managed",
        base_url="https://tsecbench.zc.tencent.com",
        token="tok",
        llm_base_url="https://api.deepseek.com/v1",
        llm_api_key="sk-test",
        llm_model="deepseek-chat",
        total_time_limit_s=21600,
        reserve_seconds=600,
        concurrency_limit=3,
        api_max_qps=3.0,
        solver_cmd=solver_cmd or [],
    )


def _installed_module_file(tmp_path: Path) -> Path:
    """模拟 /opt/tsecbench/venv/lib/python3.13/site-packages/orchestrator/main.py。"""
    p = tmp_path / "venv/lib/python3.13/site-packages/orchestrator/main.py"
    p.parent.mkdir(parents=True)
    p.write_text("", encoding="utf-8")
    return p


def test_solver_cmd_explicit_override_wins(tmp_path):
    script = tmp_path / "custom_solver.py"
    script.write_text("", encoding="utf-8")
    cmd = resolve_solver_cmd(cfg(["python3", str(script)]), environ={})
    assert cmd == ["python3", str(script)]


def test_solver_cmd_missing_script_fails_fast(tmp_path):
    env = {"TSECBENCH_SOLVER_DIR": str(tmp_path)}  # 即便有目录兜底也不该掩盖错误覆盖
    with pytest.raises(FileNotFoundError, match="solver entry not found"):
        resolve_solver_cmd(cfg(["python3", str(tmp_path / "nope.py")]), environ=env)


def test_tssecbench_solver_dir_wins_over_module_layout(tmp_path):
    solver_dir = tmp_path / "opt/tsecbench/solver"
    solver_dir.mkdir(parents=True)
    (solver_dir / "main.py").write_text("", encoding="utf-8")
    cmd = resolve_solver_cmd(
        cfg(),
        environ={"TSECBENCH_SOLVER_DIR": str(solver_dir)},
        module_file=_installed_module_file(tmp_path),
        python_exe="/opt/tsecbench/venv/bin/python",
    )
    assert cmd == ["/opt/tsecbench/venv/bin/python", str(solver_dir / "main.py")]


def test_repo_layout_fallback_still_works(tmp_path):
    repo = tmp_path / "repo"
    module = repo / "orchestrator/src/orchestrator/main.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    (repo / "solver").mkdir()
    (repo / "solver/main.py").write_text("", encoding="utf-8")
    cmd = resolve_solver_cmd(cfg(), environ={}, module_file=module, python_exe=sys.executable)
    assert cmd == [sys.executable, str(repo / "solver/main.py")]


def test_installed_layout_without_env_fails_with_actionable_error(tmp_path):
    """run-34162：site-packages 布局下 parents[3] 是 venv/lib，必须报错而不是照跑。"""
    with pytest.raises(FileNotFoundError) as exc:
        resolve_solver_cmd(cfg(), environ={}, module_file=_installed_module_file(tmp_path))
    msg = str(exc.value)
    assert "TSECBENCH_SOLVER_DIR" in msg
    assert "venv/lib/solver/main.py" in msg


def test_solver_dir_env_without_main_py_fails(tmp_path):
    empty = tmp_path / "solver"
    empty.mkdir()
    with pytest.raises(FileNotFoundError, match="solver entry not found"):
        resolve_solver_cmd(
            cfg(),
            environ={"TSECBENCH_SOLVER_DIR": str(empty)},
            module_file=_installed_module_file(tmp_path),
        )


def test_main_returns_2_before_starting_run_when_solver_missing(monkeypatch, capsys):
    async def boom(**kwargs):  # pragma: no cover - 走到这里就是失败
        raise AssertionError("run_agent must not be called")

    monkeypatch.setattr(main_mod, "resolve_config", lambda: cfg(["python3", "/no/such.py"]))
    monkeypatch.setattr(main_mod, "run_agent", boom)
    assert main_mod.main() == 2
    assert "solver entry not found" in capsys.readouterr().err
