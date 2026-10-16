"""ReActNet 图编译单测（harness-v2.md §3）。

确定性契约：同样输入产出同样图；边链完整、渲染包含逐跳指令；LLM 输出
失败（strategy 为空）时按题型默认拓扑降级。
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"


@pytest.fixture
def graphplan(monkeypatch):
    monkeypatch.syspath_prepend(str(SOLVER_DIR))
    sys.modules.pop("gov.graphplan", None)
    sys.path.insert(0, str(SOLVER_DIR / "gov"))
    try:
        yield importlib.import_module("gov.graphplan")
    finally:
        sys.path.pop(0)


def test_compile_is_deterministic(graphplan):
    task = {
        "unique_code": "g1",
        "prior": {"assets": ["10.0.0.1"], "explored": [], "open_leads": []},
    }
    a = graphplan.compile(task, "try path traversal; probe redis", "web", task["prior"])
    b = graphplan.compile(task, "try path traversal; probe redis", "web", task["prior"])
    assert a == b


def test_edges_chain_and_render(graphplan):
    task: dict = {
        "prior": {"assets": ["10.0.0.9"], "explored": ["nmap"], "open_leads": ["http://x"]}
    }
    plan = graphplan.compile(task, "probe redis for unauth", "web", task["prior"])
    ids = [node["id"] for node in plan["nodes"]]
    assert len(plan["edges"]) == len(plan["nodes"]) - 1
    for edge in plan["edges"]:
        assert edge["from"] in ids and edge["to"] in ids
        assert {"when", "why", "how"} <= set(edge)  # ReActNet 语义：何时/为何/如何
    rendered = graphplan.render(plan)
    assert "->" in rendered and "recon -> probe" in rendered


def test_unknown_category_falls_back_to_misc(graphplan):
    plan = graphplan.compile(
        {}, "", "totally-unknown", {"assets": [], "explored": [], "open_leads": []}
    )
    assert plan["category"] == "misc"
    assert plan["nodes"]


def test_recon_node_skips_fresh_scan_when_assets(graphplan):
    task = {"prior": {"assets": ["10.0.0.4:8080"], "explored": [], "open_leads": []}}
    plan = graphplan.compile(task, "", "web", task["prior"])
    recon = next(n for n in plan["nodes"] if n["id"] == "recon")
    assert "confirmed assets (1)" in recon["instruction"]


def test_dump_writes_plan_graph(graphplan, tmp_path):
    plan = graphplan.compile({}, "step one", "web", None)
    out_dir = tmp_path / "out"
    graphplan.dump(out_dir, plan)
    payload = json.loads((out_dir / "plan_graph.json").read_text(encoding="utf-8"))
    assert payload["nodes"] == plan["nodes"]
