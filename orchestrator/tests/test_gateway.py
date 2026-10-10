"""Gateway Rewriter 单测 —— 白名单 19 项逐条断言 + 边界 + 幂等（platform-api.md §4.3）。"""

import pytest
from orchestrator.gateway import is_whitelisted, rewrite_to_gateway

# platform-api.md §4.3 的 19 项白名单（agent-awd.baidu.com 是决赛项，仍在表内）
WHITELIST_HOSTS = [
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
]


class TestWhitelist:
    @pytest.mark.parametrize("host", [h for h in WHITELIST_HOSTS if h != "dashscope.aliyuncs.com"])
    def test_exact_host_accepted(self, host: str) -> None:
        assert is_whitelisted(host)

    def test_wildcard_subdomain(self) -> None:
        assert is_whitelisted("foo.maas.aliyuncs.com")
        assert is_whitelisted("a.b.maas.aliyuncs.com")

    def test_wildcard_base_itself_rejected(self) -> None:
        # 白名单写的是 *.maas.aliyuncs.com —— 裸域不在内
        assert not is_whitelisted("maas.aliyuncs.com")

    @pytest.mark.parametrize(
        "host",
        [
            "evil.example.com",
            "api.deepseek.com.evil.com",
            "deepseek.com",  # 主域不在表内
            "notdeepseek.com",
            "",  # 空 host
        ],
    )
    def test_non_whitelist_rejected(self, host: str) -> None:
        assert not is_whitelisted(host)

    def test_port_ignored_in_check(self) -> None:
        assert is_whitelisted("api.deepseek.com:443")


class TestRewrite:
    def test_deepseek_canonical(self) -> None:
        assert (
            rewrite_to_gateway("https://api.deepseek.com/v1")
            == "http://api.deepseek.com.tsecbench.gw/v1"
        )

    def test_root_url(self) -> None:
        assert (
            rewrite_to_gateway("https://api.deepseek.com") == "http://api.deepseek.com.tsecbench.gw"
        )

    def test_path_and_query_preserved(self) -> None:
        assert (
            rewrite_to_gateway("https://api.deepseek.com/v1/chat/completions?foo=1&bar=%20x")
            == "http://api.deepseek.com.tsecbench.gw/v1/chat/completions?foo=1&bar=%20x"
        )

    def test_non_default_port_preserved(self) -> None:
        assert (
            rewrite_to_gateway("https://api.deepseek.com:8443/v1")
            == "http://api.deepseek.com.tsecbench.gw:8443/v1"
        )

    def test_idempotent(self) -> None:
        once = rewrite_to_gateway("https://api.deepseek.com/v1")
        assert rewrite_to_gateway(once) == once

    def test_wildcard_host_rewrites(self) -> None:
        assert rewrite_to_gateway("https://foo.maas.aliyuncs.com/compatible-mode/v1") == (
            "http://foo.maas.aliyuncs.com.tsecbench.gw/compatible-mode/v1"
        )

    def test_http_passthrough_untouched(self) -> None:
        # 非 https（本地 mock、recorder 回流地址）不在改写职责内，原样返回
        assert rewrite_to_gateway("http://127.0.0.1:8787/x") == "http://127.0.0.1:8787/x"

    def test_non_whitelist_https_raises(self) -> None:
        with pytest.raises(ValueError, match="whitelist"):
            rewrite_to_gateway("https://evil.example.com/v1")

    def test_fragment_preserved(self) -> None:
        assert (
            rewrite_to_gateway("https://api.deepseek.com/v1#frag")
            == "http://api.deepseek.com.tsecbench.gw/v1#frag"
        )
