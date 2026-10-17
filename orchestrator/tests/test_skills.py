"""通用 skill 路由：web source/LFI、Gremlin、Gradio、逻辑 oracle。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "solver"))

import skills  # noqa: E402


def test_matches_web_source_lfi():
    skill = skills.match("合同审批 download.php?id= 文件包含")
    assert skill and skill["id"] == "web-source-lfi"
    text = skills.render(skill, "10.0.0.1:80")
    assert "download.php" in text and "STOP RULE" in text


def test_matches_gremlin_and_gradio():
    assert skills.match("HugeGraph gremlin 图数据库")["id"] == "gremlin-groovy"
    assert skills.match("Langflow Gradio :7860")["id"] == "gradio-langflow"


def test_generic_web_does_not_match():
    assert skills.match("普通登录页 SQL 注入") is None
