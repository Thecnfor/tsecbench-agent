"""Gateway Rewriter —— 全项目唯一的 LLM base_url 改写点（architecture.md §6.2）。

    https://api.deepseek.com/v1  →  http://api.deepseek.com.tsecbench.gw/v1

规则（platform-api.md §4.2/§4.3）：
  1. 域名必须在 19 项白名单内（精确或 `*.maas.aliyuncs.com` 通配）；
  2. 协议 https → http；host 追加 `.tsecbench.gw` 后缀；
  3. path / query 原样保留；端口若存在则保留；
  4. 幂等：已是 `.tsecbench.gw` 的输入原样返回。
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

GATEWAY_SUFFIX = ".tsecbench.gw"

# platform-api.md §4.3 白名单（19 项，逐字对照，不得增删）
_WHITELIST_EXACT: frozenset[str] = frozenset(
    {
        "api.hunyuan.cloud.tencent.com",
        "api.lkeap.cloud.tencent.com",
        "tokenhub.tencentmaas.com",
        "api.deepseek.com",
        "dashscope.aliyuncs.com",
        "qianfan.baidubce.com",
        "ark.cn-beijing.volces.com",
        "open.bigmodel.cn",
        "api.moonshot.cn",
        "api.siliconflow.cn",
        "spark-api-open.xf-yun.com",
        "api.minimaxi.com",
        "api.stepfun.com",
        "api.lingyiwanwu.com",
        "api.baichuan-ai.com",
        "api.xiaomimimo.com",
        "api.kimi.com",
        "agent-awd.baidu.com",
    }
)

# 白名单中唯一的通配项
_WHITELIST_WILDCARD: tuple[str, ...] = (".maas.aliyuncs.com",)


def is_whitelisted(host: str) -> bool:
    """host 是否在 19 项白名单内。输入应是裸 host（可含端口，端口被忽略）。"""
    hostname = host.rsplit(":", 1)[0].lower().rstrip(".")
    if hostname.endswith(GATEWAY_SUFFIX):
        # 已改写过的 host：按剥掉后缀再验一次，保证幂等性判定正确
        hostname = hostname[: -len(GATEWAY_SUFFIX)]
    if hostname in _WHITELIST_EXACT:
        return True
    return any(
        hostname.endswith(suffix) and hostname != suffix.lstrip(".")
        for suffix in _WHITELIST_WILDCARD
    )


def rewrite_to_gateway(url: str) -> str:
    """把白名单内的 https URL 改写为网关 URL。

    白名单外域名直接抛 ValueError（fail loud）—— 托管模式下白名单外的请求
    本来就走不通（platform-api.md §4.3），静默放行只会把失败推迟到运行期。
    非 https 协议与已是网关域名的输入原样返回（幂等）。
    """
    parts = urlsplit(url.strip())
    if parts.scheme != "https":
        return url
    hostname = (parts.hostname or "").lower()
    if hostname.endswith(GATEWAY_SUFFIX):
        return url  # 已改写，幂等
    if not is_whitelisted(hostname):
        raise ValueError(f"host not in gateway whitelist: {hostname!r} (url={url!r})")

    # 后缀落在 hostname 上，端口跟在后面：https://h:8443/p → http://h.tsecbench.gw:8443/p
    netloc = hostname + GATEWAY_SUFFIX + (f":{parts.port}" if parts.port else "")
    return urlunsplit(("http", netloc, parts.path, parts.query, parts.fragment))
