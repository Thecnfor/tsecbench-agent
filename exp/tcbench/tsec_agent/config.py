"""集中配置：双运行模式（local/hosted）+ 自动检测 + 平台凭证。

模式说明：
- local  本地模式：公网直连官方 DeepSeek API（开发调试用）
- hosted 托管模式：官方平台内网转写网关，无公网（最终交付形态，镜像 ≤3GB）
- auto   自动检测（默认）：先探测本地网关可达性，失败再试托管网关。

配置优先级：真实环境变量 > 项目根 .env > 内置默认值。
"""

import os
import re
import urllib.request
from dataclasses import dataclass, field

# --- 加载项目根 .env（不覆盖已存在的真实环境变量） ---
_DOTENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.isfile(_DOTENV):
    with open(_DOTENV, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _v = _line.split("=", 1)
            _k, _v = _k.strip(), _v.strip()
            if _k and _k not in os.environ:
                os.environ[_k] = _v


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


# --- 托管模式转写网关通用转换 ---
# 托管环境所有大模型 API 走转写网关：https://HOST[/path] → http://HOST.tsecbench.gw[/path]。
# 白名单供应商（托管环境官方清单，共19家，均可按此规则转换）：
#   api.hunyuan.cloud.tencent.com / api.lkeap.cloud.tencent.com / tokenhub.tencentmaas.com /
#   api.deepseek.com / dashscope.aliyuncs.com(compatible-mode) / *.maas.aliyuncs.com /
#   qianfan.baidubce.com / ark.cn-beijing.volces.com / open.bigmodel.cn / api.moonshot.cn /
#   api.siliconflow.cn / spark-api-open.xf-yun.com / api.minimaxi.com / api.stepfun.com /
#   api.lingyiwanwu.com / api.baichuan-ai.com / api.xiaomimimo.com / api.kimi.com /
#   agent-awd.baidu.com
# 换模型供应商时：只需改 .env 的 DEEPSEEK_BASE_URL（官方地址）+ AGENT_MODEL，
# hosted 网关地址自动转换，无需其他改动。
GATEWAY_SUFFIX = ".tsecbench.gw"


def to_gateway_url(base_url: str) -> str:
    """https://api.deepseek.com/v1 → http://api.deepseek.com.tsecbench.gw/v1（幂等）"""
    m = re.match(r"^[a-z]+://([^:/]+)(:\d+)?(/.*)?$", base_url.strip())
    if not m:
        return base_url
    host, port, path = m.group(1), m.group(2) or "", m.group(3) or ""
    if host.endswith(GATEWAY_SUFFIX):  # 已是转写网关地址，原样返回
        return base_url
    return f"http://{host}{GATEWAY_SUFFIX}{port}{path}"


# --- 两套模型后端 ---
LOCAL_BASE_URL = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
LOCAL_API_KEY = _env("DEEPSEEK_API_KEY", "")
LOCAL_MODEL = _env("AGENT_MODEL_LOCAL", "deepseek-flash")

# hosted：显式配置优先，否则由本地官方地址自动转换
HOSTED_BASE_URL = _env("DEEPSEEK_BASE_URL_HOSTED", to_gateway_url(LOCAL_BASE_URL))
HOSTED_API_KEY = _env("DEEPSEEK_API_KEY_HOSTED", "tsec-bench")
HOSTED_MODEL = _env("AGENT_MODEL_HOSTED", "deepseek-v4.1-flash-expires-on-0910")

PROBE_TIMEOUT = float(_env("AGENT_PROBE_TIMEOUT", "8"))


def _reachable(base_url: str) -> bool:
    """GET {base}/models 探测网关可达性（2xx/4xx 都算可达，只看网络层）。"""
    url = base_url.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            return 200 <= resp.status < 500
    except Exception:
        return False


def detect_mode() -> str:
    """返回最终生效模式：'local' 或 'hosted'。"""
    mode = _env("AGENT_MODE", "auto").lower()
    if mode in ("local", "hosted"):
        return mode
    # auto：先 local（公网），后 hosted（内网）
    if LOCAL_API_KEY and _reachable(LOCAL_BASE_URL):
        return "local"
    if _reachable(HOSTED_BASE_URL):
        return "hosted"
    return "local"  # 都不通时默认 local，由冒烟测试给出明确报错


def backend_for(mode: str) -> tuple[str, str, str]:
    """返回 (base_url, api_key, model)。"""
    if mode == "hosted":
        return HOSTED_BASE_URL, HOSTED_API_KEY, HOSTED_MODEL
    return LOCAL_BASE_URL, LOCAL_API_KEY, LOCAL_MODEL


@dataclass
class Config:
    # --- 运行模式（惰性解析，默认 auto 探测） ---
    mode: str = field(default="")

    # --- TSec Benchmark 平台 ---
    benchmark_token: str = _env("BENCHMARK_TOKEN", "")
    benchmark_base_url: str = _env("BENCHMARK_BASE_URL", "https://tsecbench.zc.tencent.com")
    vpn_health_url: str = _env("VPN_HEALTH_URL", "http://10.0.100.58")

    # --- 运行控制 ---
    max_turns: int = int(_env("AGENT_MAX_TURNS", "400"))
    # 分段解题：每段轮次到点 → 关闭题目释放名额、保留上下文，排到队尾稍后续跑。
    # 本题不因轮次被判死；只有上下文超模型窗口/连续收尾不通关/真失败才终止。
    task_max_turns: int = int(_env("AGENT_TASK_MAX_TURNS", "60"))
    pause_max: int = int(_env("AGENT_PAUSE_MAX", "0"))     # 每题最多暂停续跑次数，0=不限
    giveup_max: int = int(_env("AGENT_GIVEUP_MAX", "3"))   # 连续"未通关就收尾"上限，0=不限
    max_failures: int = int(_env("AGENT_MAX_FAILURES", "2"))  # 真失败放弃上限（≥1，0 按 1）
    # 只解部分题目（测试用）：逗号/空白分隔的题目编号，支持 * ? 通配；空=全部。
    # 例：AGENT_TASKS="b-01,c-0*"；命令行 --task / AGENT_TASK_FILTER 优先级更高。
    tasks: str = _env("AGENT_TASKS", "")
    deadline_sec: float = float(_env("AGENT_DEADLINE_SEC", "0"))
    request_timeout: float = float(_env("AGENT_REQUEST_TIMEOUT", "300"))
    api_max_retries: int = int(_env("AGENT_API_RETRIES", "5"))
    # LLM 重试模式：forever=网络类错误（连接/超时/限流）无限退避重试，直到任务时限；
    # finite=按 api_max_retries 次后放弃。4xx 认证/参数错误始终立即失败。
    llm_retry_mode: str = _env("AGENT_LLM_RETRY_MODE", "forever").lower()
    llm_retry_max_delay: float = float(_env("AGENT_LLM_RETRY_MAX_DELAY", "60"))

    # shell 工具
    shell_default_wait_ms: int = int(_env("AGENT_SHELL_WAIT_MS", "1500"))
    shell_max_output: int = int(_env("AGENT_SHELL_MAX_OUTPUT", "32768"))
    # 后台命令硬上限：到点整组 kill（有 out_file/日志兜底，杀掉不丢数据）；0=永不杀
    shell_hard_sec: float = float(_env("AGENT_SHELL_HARD_SEC", "1800"))
    # 同时在跑的后台 job 上限（满了拒绝新 run，提示先收/wait/kill）
    shell_max_jobs: int = int(_env("AGENT_SHELL_MAX_JOBS", "8"))

    # 追加到 shell 子进程环境的变量
    extra_env: dict = field(default_factory=lambda: {
        "BENCHMARK_TOKEN": _env("BENCHMARK_TOKEN", ""),
        "BENCHMARK_BASE_URL": _env("BENCHMARK_BASE_URL", "https://tsecbench.zc.tencent.com"),
        "NO_PROXY": "127.0.0.1",
        "no_proxy": "127.0.0.1",
    })

    # 模型后端（init 后由 _resolve 填充）
    llm_base_url: str = ""
    llm_api_key: str = ""
    model: str = ""

    def resolve(self) -> "Config":
        """确定模式并填充模型后端配置。"""
        self.mode = detect_mode()
        self.llm_base_url, self.llm_api_key, self.model = backend_for(self.mode)
        return self


CFG = Config()
