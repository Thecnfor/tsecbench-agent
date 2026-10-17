"""指纹级 FAST PATH：命中窄、步骤少、带停止规则。"""

from __future__ import annotations

import sys
from pathlib import Path

SOLVER_DIR = Path(__file__).resolve().parents[2] / "solver"
sys.path.insert(0, str(SOLVER_DIR))

import playbooks  # noqa: E402


def test_matches_known_products():
    cases = {
        "AWS Lambda 函数管理门户 云函数": "cloud-lambda-portal",
        "S3 bucket ACL public read 对象存储": "cloud-s3-public",
        "telnet :23 远程登录": "telnet-daemon",
        "Gradio :7860 应用": "gradio-app",
        "Next.js Dify :3000 门户": "nextjs-dify",
        "合同审批系统 接口 临时调整 管理员审批": "lfi-approval",
        "HugeGraph gremlin 图数据库": "gremlin-hugegraph",
        "response-builder TCP line 响应构造": "response-builder",
    }
    for text, expected in cases.items():
        assert playbooks.dump_match(text) == expected, text


def test_does_not_match_generic_web():
    assert playbooks.match("普通 Web 登录页 存在 SQL 注入 读取 flag") is None


def test_render_includes_target_and_stop_rule():
    playbook = playbooks.match("AWS Lambda 函数管理门户")
    text = playbooks.render(playbook, "10.0.0.1:8000")
    assert "# FAST PATH" in text
    assert "http://10.0.0.1:8000/api/functions" in text
    assert "STOP RULE" in text
