"""KB 查询构造：产品名扩展 + recon 指纹 + 端口。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "solver" / "kb"))

from query import build_query  # noqa: E402


def test_expands_contract_approval():
    query = build_query("公司内部合同审批系统 接口做了临时调整")
    assert "lfi" in query and "approval" in query and "auth bypass" in query


def test_expands_dify_fingerprint():
    query = build_query("AI 应用平台", '{"title":"Dify","tech":["next.js"],"url":"http://h:3000"}')
    assert "console api" in query and "next.js" in query and "port 3000" in query


def test_expands_hugegraph_and_cloud():
    assert "gremlin" in build_query("关联关系检索引擎 HugeGraph")
    assert "list-objects" in build_query("AWS S3 bucket public read")
    assert "environment variables" in build_query("AWS Lambda 配置泄露")


def test_expands_langflow():
    query = build_query("Langflow 1.2.0 AI workflow")
    assert "langflow" in query and "cve" in query


def test_generic_query_is_unchanged():
    query = build_query("普通 Web 登录页 SQL 注入")
    assert "lfi" not in query and "gremlin" not in query
