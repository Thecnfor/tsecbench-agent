"""solver 环境传递回归 —— 镜像内没有 solver/.env，LLM_* 只能来自进程环境。

run-34162 的第二个独立故障：`dict(ctx.env or {})` 在 ctx.env=None 时传了空
dict，`build_solver_env` 于是不从 os.environ 继承，solver 拿不到 LLM_API_KEY，
0.1s 内 exit 2（仓库里恰好有 solver/.env 兜底，所以本地自建靶场看不出来）。
契约：None = 继承进程环境（仍走白名单），dict = 测试/调用方显式注入。
"""

from __future__ import annotations

import asyncio
import sys
import time

from orchestrator.config import RuntimeConfig
from orchestrator.main import REASONER_MODEL, solver_env_for
from orchestrator.registry import ChallengeRecord
from orchestrator.supervisor import run_solver


def rec(**kw) -> ChallengeRecord:
    kw.setdefault("unique_code", "t1")
    kw.setdefault("container_addr", ["127.0.0.1:80"])
    return ChallengeRecord(**kw)


def test_none_env_inherits_process_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_API_KEY", "sk-from-process")
    cmd = [sys.executable, "-c", "import os; print(os.environ.get('LLM_API_KEY', ''))"]
    outcome = asyncio.run(
        run_solver(
            solver_cmd=cmd,
            ws=tmp_path,
            deadline_epoch=time.time() + 30,
            slice_s=10,
            environ=solver_env_for(None, rec(difficulty="easy")),
        )
    )
    assert outcome.exit_code == 0
    assert (tmp_path / "stdout.log").read_text(encoding="utf-8").strip() == "sk-from-process"


def test_hard_upgrade_keeps_inherited_env_and_sets_model(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-from-process")
    env = solver_env_for(None, rec(difficulty="hard"))
    assert env is not None
    assert env["LLM_API_KEY"] == "sk-from-process"
    assert env["LLM_MODEL"] == REASONER_MODEL


def test_explicit_env_is_passed_through():
    env = solver_env_for({"LLM_API_KEY": "sk-injected"}, rec(difficulty="easy"))
    assert env == {"LLM_API_KEY": "sk-injected"}


def test_gateway_rewritten_url_reaches_solver_env():
    """托管模式：config.llm_base_url 是 rewrite_to_gateway 的结果，必须注入 solver。"""
    cfg = RuntimeConfig(
        mode="managed",
        base_url="https://tsecbench.zc.tencent.com",
        token="tok",
        llm_base_url="http://api.deepseek.com.tsecbench.gw:8080/v1",
        llm_api_key="sk-config",
        llm_model="deepseek-chat",
        total_time_limit_s=60,
        reserve_seconds=10,
        concurrency_limit=3,
        api_max_qps=3.0,
    )
    env = solver_env_for(None, rec(difficulty="easy"), cfg)
    assert env is not None
    assert env["LLM_BASE_URL"] == "http://api.deepseek.com.tsecbench.gw:8080/v1"
    assert env["LLM_API_KEY"] == "sk-config"
    assert env["LLM_MODEL"] == "deepseek-chat"


def test_hard_upgrade_wins_over_config_model():
    cfg = RuntimeConfig(
        mode="managed",
        base_url="",
        token="",
        llm_base_url="http://gw/v1",
        llm_api_key="sk",
        llm_model="deepseek-chat",
        total_time_limit_s=60,
        reserve_seconds=10,
        concurrency_limit=3,
        api_max_qps=3.0,
    )
    env = solver_env_for(None, rec(difficulty="hard"), cfg)
    assert env is not None and env["LLM_MODEL"] == REASONER_MODEL


def test_reasoner_only_for_long_hard_slice():
    base = {"LLM_MODEL": "deepseek-chat"}
    assert solver_env_for(base, rec(difficulty="hard"), slice_s=300)["LLM_MODEL"] != REASONER_MODEL
    assert solver_env_for(base, rec(difficulty="hard"), slice_s=900)["LLM_MODEL"] == REASONER_MODEL
    assert (
        solver_env_for(base, rec(difficulty="easy", attempts=3), slice_s=900)["LLM_MODEL"]
        != REASONER_MODEL
    )
