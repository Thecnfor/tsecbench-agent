"""findings 提取 —— 服务多阶段/重投的结构化状态，不靠模型记忆。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


def load_state():
    sys.path.insert(0, str(SOLVER_DIR))
    sys.modules.pop("state", None)
    return importlib.import_module("state")


def test_extracts_credentials_and_endpoints():
    state = load_state()
    text = (
        '{"DATABASE_PASSWORD":"S3cretPwd!","DATABASE_USER":"dbadmin"}\n'
        "GET http://10.0.0.5:8080/api/functions/order/config\n"
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij\n"
        "Server: nginx/1.18.0\n"
    )
    out = state.extract_findings(text)
    assert any("S3cretPwd!" in item for item in out["credentials"])
    assert any("10.0.0.5:8080" in item for item in out["endpoints"])
    assert any("nginx/1.18.0" in item for item in out["versions"])


def test_synthesize_prior_keeps_contract_keys():
    state = load_state()
    events = [
        {"text": "$ curl -i http://10.0.0.5:8080/\nHTTP/1.1 200 OK"},
        {"text": "admin:Passw0rd@10.0.0.6"},
    ]
    prior = state.synthesize_prior(events, candidates=[])
    for key in ("assets", "explored", "open_leads", "credentials", "findings"):
        assert key in prior and isinstance(prior[key], list)
    assert prior["explored"] == ["curl -i http://10.0.0.5:8080/"]
    assert any("admin:Passw0rd" in item for item in prior["credentials"])


def test_append_findings_is_deduplicated(tmp_path):
    state = load_state()
    path = tmp_path / "findings.txt"
    state.append_findings(path, {"credentials": ["admin:pass"], "endpoints": []})
    state.append_findings(path, {"credentials": ["admin:pass"], "endpoints": []})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == ["credentials: admin:pass"]
