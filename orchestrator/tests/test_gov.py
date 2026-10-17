"""solver/gov/ 治理层单测（离线，无 LLM）。

session：JSONL 事件写入与读回容错；
profiles：确定性题型分类与策略包；
planner：跳过条件（无 key / 时间不足）；
prior：事件流 → prior.json 的确定性合成。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


@pytest.fixture
def gov_modules(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("common", None)
    common = importlib.import_module("common")  # gov 模块路径依赖
    session = importlib.import_module("gov.session")
    profiles = importlib.import_module("gov.profiles")
    planner = importlib.import_module("gov.planner")
    prior = importlib.import_module("gov.prior")
    yield {
        "common": common,
        "session": session,
        "profiles": profiles,
        "planner": planner,
        "prior": prior,
        "tmp": tmp_path,
    }
    sys.modules.pop("gov.session", None)
    sys.modules.pop("gov.profiles", None)
    sys.modules.pop("gov.planner", None)
    sys.modules.pop("gov.prior", None)
    sys.modules.pop("gov", None)
    sys.modules.pop("common", None)


# ------------------------------------------------------------------ session ---
def test_session_roundtrip_and_tolerates_corruption(gov_modules):
    mod, tmp = gov_modules["session"], gov_modules["tmp"]
    sess = mod.Session(tmp / "out")
    sess.log("meta", solver="x")
    sess.log("candidate", flag="FLAG{a}")
    sess.close()
    # 追加一行坏数据（模拟进程被杀时写一半）
    with (tmp / "out" / "session.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "trunc')
    events = mod.Session(tmp / "out").lines()
    assert [e["kind"] for e in events] == ["meta", "candidate"]
    assert events[1]["flag"] == "FLAG{a}"


# ----------------------------------------------------------------- profiles ---
def test_profiles_classify_deterministic(gov_modules):
    mod = gov_modules["profiles"]
    assert mod.classify("exploit the http login form, sqli in the url parameter") == "web"
    assert mod.classify("stack overflow in the elf binary, glibc heap") == "pwn"
    assert mod.classify("decompile with ghidra, obfuscated crackme") == "reverse"
    assert mod.classify("recover data from the pcap with wireshark") == "forensics"
    assert mod.classify("rsa cipher with small e") in ("misc", "web")
    assert mod.classify("") == "misc"


def test_profiles_select_returns_strategy(gov_modules):
    mod = gov_modules["profiles"]
    cat, strategy = mod.select(
        {"description": "http service with login form", "targets": ["10.0.0.1:80"]}
    )
    assert cat == "web"
    assert "WEB STRATEGY" in strategy


# ------------------------------------------------------------------ planner ---
def test_planner_skips_without_api_key(gov_modules, monkeypatch):
    mod = gov_modules["planner"]
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert mod.plan({"description": "x"}, None) is None


def test_planner_skips_when_time_short(gov_modules, monkeypatch):
    mod = gov_modules["planner"]
    monkeypatch.setenv("LLM_API_KEY", "sk-fake")
    import time

    assert mod.plan({"description": "x"}, time.time() + 30) is None


# -------------------------------------------------------------------- prior ---
def test_prior_synthesize_and_write(gov_modules):
    smod, pmod, tmp = gov_modules["session"], gov_modules["prior"], gov_modules["tmp"]
    sess = smod.Session(tmp / "out")
    sess.log("pi_stdout", text="$ curl -i http://10.0.0.1:8080/admin")
    sess.log("pi_stdout", text="found link https://10.0.0.1:8080/backup.zip")
    sess.log("candidate", flag="FLAG{a}")
    out = pmod.write(tmp / "out", sess.lines(), ["FLAG{a}"])
    sess.close()
    assert out is not None
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "10.0.0.1:8080" in " ".join(data["assets"])
    assert any("curl" in c for c in data["explored"])
    # 已产出 flag 的线索不算 open lead；backup.zip 未出 flag → open lead
    assert any("backup.zip" in u for u in data["open_leads"])


def test_prior_synthesize_includes_credentials(gov_modules):
    prior = gov_modules["prior"]
    result = prior.synthesize([{"text": '{"DATABASE_PASSWORD":"flag{pwd_test}"}'}], candidates=[])
    assert "credentials" in result
    assert any("flag{pwd_test}" in item for item in result["credentials"])
