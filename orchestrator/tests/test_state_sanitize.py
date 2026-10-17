"""静态 JS/HTML 字符串不能污染 assets/credentials（两段 300s 空窗的根因）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "solver"))

import state  # noqa: E402


def test_static_js_strings_are_not_assets_or_credentials():
    text = (
        "password: p.Yj().min(8,{message: ...; font-family: Chivo:ital,wght; "
        "password=Passw0rd!123; 10.0.186.192:5000; apps.html; www.w3.org; 127.0.0.1:5001"
    )
    findings = state.extract_findings(text)
    assert "Passw0rd!123" in findings["credentials"]
    assert not any("Yj" in item for item in findings["credentials"])
    assert not any("Chivo" in item for item in findings["credentials"])
    assert any("10.0.186.192:5000" in item for item in findings["endpoints"])
    assert not any("apps.html" in item for item in findings["endpoints"])
    assert not any("127.0.0.1" in item for item in findings["endpoints"])
    assert not any("www.w3.org" in item for item in findings["endpoints"])


def test_synthesize_prior_filters_static_assets():
    prior = state.synthesize_prior(
        [{"text": "apps.html www.w3.org 10.0.186.192:3000 127.0.0.1:5001"}], []
    )
    assert "10.0.186.192:3000" in prior["assets"]
    assert not any("apps.html" in item for item in prior["assets"])
    assert not any("127.0.0.1" in item for item in prior["assets"])
    assert not any("www.w3.org" in item for item in prior["assets"])


def test_host_docker_ranges_are_not_assets():
    assert state._asset_ok("172.18.0.1:8080") is False
    assert state._asset_ok("127.0.0.1:8080") is False
    assert state._asset_ok("10.0.168.1:80") is True
