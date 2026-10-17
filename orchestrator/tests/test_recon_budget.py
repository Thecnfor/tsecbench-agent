"""recon 预算与 URL 推导（轨迹证据：85s 片内固定侦察开销 + mode=all 参数错误）。"""

from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


def load_recipe():
    sys.path.insert(0, str(SOLVER_DIR))
    sys.modules.pop("recon_recipe", None)
    return importlib.import_module("recon_recipe")


def test_resolve_budget_prefers_explicit():
    rr = load_recipe()
    assert rr.resolve_budget_seconds(42) == 42


def test_resolve_budget_reads_deadline_env(monkeypatch):
    rr = load_recipe()
    monkeypatch.setenv("SOLVER_DEADLINE_EPOCH", str(time.time() + 30))
    budget = rr.resolve_budget_seconds()
    assert budget is not None and 20 <= budget <= 31


def test_resolve_budget_absent(monkeypatch):
    rr = load_recipe()
    monkeypatch.delenv("SOLVER_DEADLINE_EPOCH", raising=False)
    assert rr.resolve_budget_seconds() is None


def test_derive_web_url_prefers_common_ports():
    rr = load_recipe()
    assert rr.derive_web_url("h", ["8080"]) == "http://h:8080"
    assert rr.derive_web_url("h", ["443"]) == "https://h"
    assert rr.derive_web_url("h", ["8443"]) == "https://h:8443"
    assert rr.derive_web_url("h", []) == "http://h"


def test_low_budget_skips_dirbust(monkeypatch):
    rr = load_recipe()
    monkeypatch.setattr(
        rr, "_which", lambda name: "feroxbuster" if name == "feroxbuster" else "curl"
    )
    monkeypatch.setattr(rr, "_curl_page", lambda curl, url: "HTTP/1.1 200 OK\r\nServer: x\r\n\r\n")
    called = []
    monkeypatch.setattr(rr, "_run", lambda argv, timeout=120: called.append(argv) or "")
    result = rr.recon_web("http://h:8080", budget_s=5)
    assert result["dirbust_skipped"] == "low-budget"
    assert not any("feroxbuster" in " ".join(map(str, argv)) for argv in called)


def test_low_budget_uses_small_nmap(monkeypatch):
    rr = load_recipe()
    monkeypatch.setattr(rr, "_which", lambda name: "nmap")
    seen = {}

    def fake_run(argv, timeout=120):
        seen["argv"] = argv
        seen["timeout"] = timeout
        return "80/tcp open http nginx 1.18.0\n"

    monkeypatch.setattr(rr, "_run", fake_run)
    rr.recon_host("10.0.0.1", budget_s=10)
    assert "--top-ports" in seen["argv"] and "100" in seen["argv"]
    assert seen["timeout"] <= 10


def test_parse_dirbust_lines_accepts_feroxbuster_urls():
    rr = load_recipe()
    lines = rr.parse_dirbust_lines(
        "feroxbuster",
        "http://h:8080/admin\nhttp://h:8080/login?next=/\n",
    )
    assert lines == ["http://h:8080/admin", "http://h:8080/login?next=/"]


def test_parse_dirbust_lines_keeps_gobuster_entries():
    rr = load_recipe()
    line = "/admin                (Status: 301) [Size: 312]"
    assert rr.parse_dirbust_lines("gobuster", line + "\n\n") == [line]
