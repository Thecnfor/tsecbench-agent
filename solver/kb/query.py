"""KB 查询构造：题面 + recon 指纹 + 产品名扩展。

实测（run-35236）纯题面检索会漏掉产品知识；把 Dify/HugeGraph/S3/Lambda
等别名展开后，curated wiki 的命中率明显提高。零依赖、纯字符串。
"""

from __future__ import annotations

import re

# (触发词, 扩展词)。触发词命中任意一个即追加该组扩展。
ALIASES: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("dify",), ("next.js", "console api", "ssrf", "rsc", "server action")),
    (("gitea", "forgejo"), ("gitea", "oauth", "pow", "registration", "cve")),
    (("flask", "werkzeug"), ("flask", "werkzeug", "ssti", "debug console", "lfi")),
    (("azurite", "minio"), ("azure", "sas", "blob", "storage", "azurite")),
    (("jenkins",), ("jenkins", "script console", "cve")),
    (("hugegraph", "gremlin", "图数据库"), ("gremlin", "tinkerpop", "janusgraph", "auth bypass")),
    (("s3", "bucket", "对象存储"), ("s3", "bucket acl", "public-read", "list-objects")),
    (
        ("lambda", "云函数", "serverless"),
        ("lambda", "serverless", "environment variables", "config"),
    ),
    (("gradio",), ("gradio", "7860", "config api", "predict")),
    (("telnet", "远程登录"), ("telnet", "default credentials", "banner")),
    (("合同审批", "审批"), ("lfi", "file inclusion", "approval", "idor", "auth bypass")),
    (("response-builder", "响应构造"), ("memory leak", "protocol fuzz", "binary")),
    (("next.js", "__next", "nextjs"), ("next.js", "rsc", "server action")),
]

_PORT_RE = re.compile(r":(\d{2,5})")


def build_query(text: str, fingerprint: str = "", *, limit: int = 500) -> str:
    """题面 + 指纹 + 产品扩展；去重后截断到 limit。"""
    combined = f"{text} {fingerprint}".lower()
    parts: list[str] = [text.strip(), fingerprint.strip()]
    for triggers, terms in ALIASES:
        if any(trigger in combined for trigger in triggers):
            parts.extend(terms)
    ports = _PORT_RE.findall(fingerprint)
    for port in dict.fromkeys(ports):
        parts.append(f"port {port}")
    words = " ".join(part for part in parts if part).split()
    return " ".join(dict.fromkeys(words))[:limit]
