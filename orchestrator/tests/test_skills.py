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


def test_description_specific_skills():
    cases = {
        "pydash 原型链 Cookie 八进制 路径解析": "proto-pollution-pydash",
        "SSRF internal-api 内网 诊断工具": "ssrf-internal",
        "XML 批量导入 图片上传": "xxe-import",
        ".tpl 模板解析 反序列化": "template-unserialize",
        "1panel 服务器管理控制面板": "1panel",
        "泛微 OA SSH": "weaver-oa",
        "SAS 临时访问凭证 Azure": "azure-sas",
        "固件 可执行文件 逆向 许可证": "firmware-re",
        "检测机制 规避 实现质量": "detection-bypass",
        "官网 门户网站 内网 APT 全链路": "multi-stage-web",
    }
    for text, expected in cases.items():
        assert skills.match(text)["id"] == expected, text
