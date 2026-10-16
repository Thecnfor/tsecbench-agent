"""Config Resolver 单测 —— 双模解析（§6.1）+ solver 环境白名单（§7.1 #21）。"""

from __future__ import annotations

import pytest
from orchestrator.config import RuntimeConfig, build_solver_env, resolve_config
from orchestrator.ingest import resolve_ingest

TOKEN = "tok-abcdef1234567890"
BASE = "https://tsecbench.zc.tencent.com"

BASE_ENV = {
    "BENCHMARK_TOKEN": TOKEN,
    "BENCHMARK_BASE_URL": BASE,
    "LLM_API_KEY": "sk-test",
}


def test_solver_env_whitelist_excludes_token():
    """§7.1 #21：solver 环境绝不含 BENCHMARK_TOKEN，只含白名单变量。"""
    env = build_solver_env(
        {
            "BENCHMARK_TOKEN": "MUST-NOT-LEAK",
            "LLM_API_KEY": "sk-x",
            "LLM_BASE_URL": "http://gw/v1",
            "SOLVER_TASK": "/tmp/task.json",
            "HTTP_PROXY": "http://127.0.0.1:8888",
            "PATH": "/usr/bin",
            "SOMETHING_ELSE": "no",
        },
        extra={"SOLVER_DEADLINE_EPOCH": "1234"},
    )
    assert "BENCHMARK_TOKEN" not in env
    assert "SOMETHING_ELSE" not in env
    assert env["LLM_API_KEY"] == "sk-x"
    assert env["SOLVER_TASK"] == "/tmp/task.json"
    assert env["HTTP_PROXY"] == "http://127.0.0.1:8888"
    assert env["SOLVER_DEADLINE_EPOCH"] == "1234"
    # 白名单是构造式的：任何 LLM_*/SOLVER_* 前缀都过，但平台键名前缀不在白名单
    assert not any(k.startswith("BENCHMARK") for k in env)


def test_solver_env_never_contains_token_even_in_extra():
    with pytest.raises(AssertionError):
        build_solver_env({}, extra={"BENCHMARK_TOKEN": "leak"})


def test_config_local_mode_direct_llm(tmp_path):
    env = {
        **BASE_ENV,
        "GATEWAY_MODE": "off",
        "LLM_BASE_URL": "https://api.deepseek.com/v1",
    }
    cfg = resolve_config(str(tmp_path), environ=env)
    assert cfg.mode == "managed"  # 无 ovpn 下发 → 视为托管
    assert cfg.llm_base_url == "https://api.deepseek.com/v1"  # off 不改写
    assert cfg.token == TOKEN
    assert cfg.concurrency_limit == 3


def test_config_managed_mode_rewrites_gateway(tmp_path):
    env = {
        **BASE_ENV,
        "GATEWAY_MODE": "on",
        "LLM_BASE_URL": "https://api.deepseek.com/v1",
    }
    cfg = resolve_config(str(tmp_path), environ=env)
    assert cfg.llm_base_url == "http://api.deepseek.com.tsecbench.gw/v1"


def test_config_local_mode_with_ovpn(tmp_path):
    ing = tmp_path / "ing"
    ing.mkdir()
    (ing / "env").write_text(f"BENCHMARK_TOKEN={TOKEN}\nBENCHMARK_BASE_URL={BASE}")
    (ing / "task_x_vpn.ovpn").write_text(
        "client\ndev tun\n<ca>\n</ca>\n<key>\n</key>", encoding="utf-8"
    )
    cfg = resolve_config(str(ing), environ={"LLM_API_KEY": "sk-test", "GATEWAY_MODE": "off"})
    assert cfg.mode == "local"
    assert cfg.vpn_configs and cfg.vpn_configs[0].endswith(".ovpn")


def test_config_missing_llm_key_hard_fails(tmp_path):
    env = {k: v for k, v in BASE_ENV.items() if k != "LLM_API_KEY"}
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        resolve_config(str(tmp_path), environ=env)


def test_config_missing_credentials_hard_fails(tmp_path):
    with pytest.raises(ValueError, match="no platform credentials"):
        resolve_config(str(tmp_path), environ={"LLM_API_KEY": "sk-test"})


def test_config_concurrency_cap(tmp_path):
    with pytest.raises(ValueError, match="3"):
        resolve_config(str(tmp_path), environ={**BASE_ENV, "CONCURRENCY_LIMIT": "4"})


def test_config_defaults(tmp_path):
    cfg = resolve_config(str(tmp_path), environ=dict(BASE_ENV))
    assert cfg.total_time_limit_s == 21600
    assert cfg.reserve_seconds == 600
    assert cfg.api_max_qps == 3.0
    assert cfg.llm_model == "deepseek-chat"
    assert isinstance(cfg, RuntimeConfig)


def test_ingest_result_has_no_public_token_in_report(tmp_path):
    result = resolve_ingest(environ=dict(BASE_ENV))
    assert result.report.token_found is True
    assert result.token == TOKEN  # token 只在 L0 返回值


class TestHarnessV2Flag:
    """v2 特性开关：模块级读 env（v2 总闸，默认 off，旁路零回归）。"""

    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("HARNESS_V2", raising=False)
        from orchestrator.config import HARNESS_V2

        assert HARNESS_V2 is False

    def test_on(self, monkeypatch):
        import importlib

        from orchestrator import config

        monkeypatch.setenv("HARNESS_V2", "on")
        importlib.reload(config)
        try:
            assert config.HARNESS_V2 is True
        finally:
            # reload 回默认态，避免污染其他测试看到的模块级常量
            monkeypatch.delenv("HARNESS_V2", raising=False)
            importlib.reload(config)
            assert config.HARNESS_V2 is False
