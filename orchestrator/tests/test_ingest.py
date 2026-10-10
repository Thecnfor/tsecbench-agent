"""Ingest Resolver 单测 —— architecture.md §7.1 摄取用例矩阵 #1~#20。

每条都是确定性断言，把「下次下发形状不确定」提前枚举掉（§7.1 前言）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from orchestrator.ingest import (
    classify_content,
    parse_json_config,
    parse_kv_line,
    parse_kv_text,
    resolve_ingest,
)

TOKEN = "tok-1234567890abcdef"
BASE = "https://tsecbench.zc.tencent.com"


def write_ing(tmp_path: Path, files: dict[str, str]) -> Path:
    ing = tmp_path / "ing"
    ing.mkdir(exist_ok=True)
    for name, content in files.items():
        p = ing / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return ing


# ------------------------------------------------------- 解析原语（#1~#11）----
def test_01_colon_no_space():
    assert parse_kv_line("KEY:VALUE") == ("KEY", "VALUE")


def test_02_colon_with_space():
    assert parse_kv_line("KEY: VALUE") == ("KEY", "VALUE")  # 值不带前导空格


def test_03_equals_sign():
    assert parse_kv_line("KEY=VALUE") == ("KEY", "VALUE")  # 与 #1 等价


def test_04_value_contains_colon():
    """回归历史错误：值内含 https:// 不得被切成 https。"""
    assert parse_kv_line("KEY:https://host/x") == ("KEY", "https://host/x")


def test_05_value_contains_equals():
    assert parse_kv_line("KEY=http://h/p?a=1&b=2") == ("KEY", "http://h/p?a=1&b=2")


def test_06_crlf():
    assert parse_kv_line("KEY:value\r\n") == ("KEY", "value")


def test_07_utf8_bom():
    assert parse_kv_line("\ufeffKEY:value") == ("KEY", "value")


def test_08_blank_comment_trailing_ws():
    assert parse_kv_line("") is None
    assert parse_kv_line("   ") is None
    assert parse_kv_line("# comment") is None
    assert parse_kv_line("KEY:value   ") == ("KEY", "value")


def test_09_no_trailing_newline():
    values, _ = parse_kv_text("A:1\nB:2")  # 末行无换行
    assert values == {"A": "1", "B": "2"}


def test_10_quoted_value():
    assert parse_kv_line('KEY="v"') == ("KEY", "v")
    assert parse_kv_line("KEY='v'") == ("KEY", "v")


def test_11_duplicate_key_warns():
    values, warns = parse_kv_text("K:first\nK:second")
    assert values == {"K": "second"}  # 后者优先
    assert warns and "duplicate" in warns[0]


# --------------------------------------------------- 别名与分类（#12~#14）----
def test_12_legacy_alias_names(tmp_path):
    """键名换代：AGENT_TOKEN / SERVER_HOST 映射到内部规范键。"""
    write_ing(tmp_path, {"env": "AGENT_TOKEN:ignored\nSERVER_HOST:ignored"})
    result = resolve_ingest(
        str(tmp_path / "ing"),
        environ={"AGENT_TOKEN": TOKEN, "SERVER_HOST": BASE},
    )
    assert result.token == TOKEN
    assert result.base_url == BASE


def test_13_unknown_key_preserved_in_report(tmp_path):
    """未识别键不报错、不丢弃，保留进报告（格式变化预警）。"""
    write_ing(
        tmp_path,
        {"env": f"BENCHMARK_TOKEN:{TOKEN}\nBENCHMARK_BASE_URL:{BASE}\nNEW_FEATURE_KEY:xyz"},
    )
    result = resolve_ingest(str(tmp_path / "ing"), environ={})
    assert result.token == TOKEN
    assert "NEW_FEATURE_KEY" in result.report.unknown_keys


def test_14_multiple_ovpn_deterministic(tmp_path):
    ovpn = (
        "client\ndev tun\n<ca>\n-----BEGIN CERTIFICATE-----\nabc\n"
        "-----END CERTIFICATE-----\n</ca>\n<key>\n"
        "-----BEGIN PRIVATE KEY-----\nk\n-----END PRIVATE KEY-----\n</key>"
    )
    write_ing(
        tmp_path,
        {
            "task_b_vpn.ovpn": ovpn,
            "task_a_vpn.ovpn": ovpn,
            "env": f"BENCHMARK_TOKEN:{TOKEN}\nBENCHMARK_BASE_URL:{BASE}",
        },
    )
    result = resolve_ingest(str(tmp_path / "ing"), environ={})
    assert [Path(p).name for p in result.vpn_configs] == [
        "task_a_vpn.ovpn",
        "task_b_vpn.ovpn",
    ]  # 确定性排序，不崩
    assert result.token == TOKEN


# ------------------------------------------------------- 目录级（#15~#17）----
def test_15_no_ing_dir_is_managed_mode(tmp_path):
    """完全没有 ing/：托管模式，全走环境变量，是合法状态。"""
    result = resolve_ingest(
        str(tmp_path / "nonexistent"),
        environ={"BENCHMARK_TOKEN": TOKEN, "BENCHMARK_BASE_URL": BASE},
    )
    assert result.token == TOKEN and result.vpn_configs == []


def test_16_ing_without_ovpn_soft_start(tmp_path):
    """ing/ 存在但缺 ovpn：软启动（§6.3 走可达性探测）。"""
    write_ing(tmp_path, {"env": f"BENCHMARK_TOKEN={TOKEN}\nBENCHMARK_BASE_URL={BASE}"})
    result = resolve_ingest(str(tmp_path / "ing"), environ={})
    assert result.vpn_configs == []


def test_17_empty_env_hard_fails(tmp_path):
    """ing/ 存在但 env 为空文件：硬失败，不发空 URL 请求。"""
    ing = write_ing(tmp_path, {"env": ""})
    with pytest.raises(ValueError, match="no platform credentials"):
        resolve_ingest(str(ing), environ={})


# ------------------------------------------------- 分类与误判（#18~#19）------
def test_18_content_signature_beats_extension(tmp_path):
    """内容签名优先于扩展名：ovpn 内容像 env / env 内容像 ovpn 时分类正确。"""
    assert classify_content("client\ndev tun\n<ca>\n<cert>\n<key>\n") == "ovpn"
    assert classify_content("A=1\nB=2\nC=3\n") == "env"
    # 名字叫 .ovpn 但内容是键值 → 按 env 解析（排序在 env 之前，env 文件的值胜出）
    ing = write_ing(
        tmp_path,
        {
            "aaa_weird.ovpn": "BENCHMARK_TOKEN:tok-x\nBENCHMARK_BASE_URL:http://h",
            "env": f"BENCHMARK_TOKEN:{TOKEN}\nBENCHMARK_BASE_URL:{BASE}",
        },
    )
    result = resolve_ingest(str(ing), environ={})
    assert result.token == TOKEN  # 正常摄取，未把键值文件当 ovpn 忽略
    assert any("cross-file duplicate" in w for w in result.report.warnings)


def test_19_unknown_and_ca_files_ignored(tmp_path):
    """未知类型（二进制）/ CA 证书 / JSON：按分类表处理，未知即忽略 + 报告。"""
    ing = write_ing(
        tmp_path,
        {
            "blob.bin": "\x00\x01\x02binary",
            "ca.pem": "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----",
            "cfg.json": f'{{"BENCHMARK_TOKEN": "tok-json", "BENCHMARK_BASE_URL": "{BASE}"}}',
        },
    )
    result = resolve_ingest(str(ing), environ={})
    assert result.token == "tok-json"  # JSON 配置被递归展平摄取
    kinds = {Path(f["path"]).name: f["kind"] for f in result.report.files}
    assert kinds["blob.bin"] == "unknown"
    assert kinds["ca.pem"] == "ca"
    assert kinds["cfg.json"] == "json"


def test_19b_json_config_flattens(tmp_path):
    ing = write_ing(
        tmp_path,
        {
            "cfg.json": f'{{"platform": {{"token": "tok-nested", "base_url": "{BASE}"}}}}',
            "env": f"BENCHMARK_TOKEN:{TOKEN}\nBENCHMARK_BASE_URL:{BASE}",
        },
    )
    result = resolve_ingest(str(ing), environ={})
    # 展平后键名 platform.token 不在别名表 → env 补齐；未识别键保留进报告
    assert result.token == TOKEN
    assert result.report.unknown_keys


# --------------------------------------------------------------- #20 脱敏 ----
def test_20_report_is_masked(tmp_path):
    """ingest_report 中不得出现 token 明文/私钥内容。"""
    ing = write_ing(
        tmp_path,
        {
            "env": f"BENCHMARK_TOKEN:{TOKEN}\nBENCHMARK_BASE_URL:{BASE}",
            "vpn.ovpn": (
                "client\n<ca>\nx\n</ca>\n<key>\n"
                "-----BEGIN PRIVATE KEY-----\nSECRETKEY\n-----END PRIVATE KEY-----\n</key>"
            ),
        },
    )
    result = resolve_ingest(str(ing), environ={})
    import json
    from dataclasses import asdict

    dumped = json.dumps(asdict(result.report), default=str)
    assert TOKEN not in dumped
    assert "SECRETKEY" not in dumped
    assert result.report.token_found is True


# ------------------------------------------------------- 合并与坏值拒绝 ----
def test_priority_env_over_ing(tmp_path):
    ing = write_ing(tmp_path, {"env": f"BENCHMARK_TOKEN:{TOKEN}\nBENCHMARK_BASE_URL:{BASE}"})
    result = resolve_ingest(str(ing), environ={"BENCHMARK_TOKEN": "tok-env-wins"})
    assert result.token == "tok-env-wins"
    assert any("env overrides ing" in w for w in result.report.warnings)


def test_truncated_base_url_rejected(tmp_path):
    """明显截断的 base_url（不含 ://）拒绝采用 → 硬失败并记警告。"""
    ing = write_ing(tmp_path, {"env": f"BENCHMARK_TOKEN:{TOKEN}\nBENCHMARK_BASE_URL:https"})
    with pytest.raises(ValueError, match="no platform credentials"):
        resolve_ingest(str(ing), environ={})
    # 同场景但 env 提供合法值 → 回退成功
    result = resolve_ingest(
        str(ing), environ={"BENCHMARK_BASE_URL": BASE, "BENCHMARK_TOKEN": TOKEN}
    )
    assert result.base_url == BASE


def test_json_config_primitives():
    assert parse_json_config('{"a": {"b": 1}}') == {"a.b": "1"}
    assert parse_json_config("not json") is None
    assert parse_json_config("[1,2]") is None


def test_hard_fail_lists_both_missing():
    with pytest.raises(ValueError, match="BENCHMARK_TOKEN.*BENCHMARK_BASE_URL"):
        resolve_ingest("/nonexistent-dir-for-test", environ={})
