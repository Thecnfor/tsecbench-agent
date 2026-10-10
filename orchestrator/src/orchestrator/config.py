"""Config Resolver —— 双模配置来源（architecture.md §6.1）。

本地评测：解析 `ing/env`（冒号分隔）、宿主机拨 VPN、LLM 直连真实 DeepSeek。
托管沙箱：平台注入环境变量、推定平台侧已打通 VPN、LLM 走网关改写。
差异全部收敛到启动期配置解析（ingest.resolve_ingest），业务代码零分支。

本模块额外固化 §6.5.1 的硬约束：**solver 环境白名单构造，绝不包含
BENCHMARK_TOKEN**（靶场内容对手可控，提示注入可读环境变量 —— 少传一个
变量，注入能拿到的最多是 LLM key 而不是平台 token）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .gateway import rewrite_to_gateway
from .ingest import IngestResult, resolve_ingest

# solver 子进程环境白名单（§6.5.1 / §7.1 #21）。
# 规则：白名单构造，不是继承 + 覆盖。
#   - LLM_* / SOLVER_*：solver 运行所需；
#   - 代理与 CA：强制录制路径（architecture.md §2.1）；
#   - 系统必需（PATH/HOME/TEMP 等）：子进程能启动的最低限度。
SOLVER_ENV_BASE = (
    "PATH",
    "HOME",
    "TEMP",
    "TMP",
    "SYSTEMROOT",  # Windows 子进程（python.exe 找 DLL）必需
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
)
SOLVER_ENV_PREFIXES = ("LLM_", "SOLVER_")
SOLVER_ENV_EXACT = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "RECORDER_STATE",
)


def build_solver_env(
    environ: dict[str, str] | None = None, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """构造 solver 子进程环境（白名单）。保证不含 BENCHMARK_TOKEN / 平台凭据。"""
    src = dict(os.environ if environ is None else environ)
    env: dict[str, str] = {}
    for key in SOLVER_ENV_BASE:
        if key in src:
            env[key] = src[key]
    for key, value in src.items():
        if key.startswith(SOLVER_ENV_PREFIXES):
            env[key] = value
    for key in SOLVER_ENV_EXACT:
        if key in src:
            env[key] = src[key]
    if extra:
        env.update(extra)
    # 白名单的回归防线：平台凭据混进来是测试失败，不是静默通过
    assert "BENCHMARK_TOKEN" not in env, "solver env must never contain BENCHMARK_TOKEN"
    return env


@dataclass(slots=True)
class RuntimeConfig:
    """启动期一次性解析产物；之后全项目只读它。"""

    mode: str  # "local" | "managed"
    base_url: str  # 平台 API base（未改写，API client 用）
    token: str  # 只留在 L0
    llm_base_url: str  # 已按网关规则改写完成的最终 base_url
    llm_api_key: str
    llm_model: str
    total_time_limit_s: int
    reserve_seconds: int
    concurrency_limit: int
    api_max_qps: float
    hint_cost_threshold: float
    slice_min_s: int = 120  # 每题时间片下限（秒）
    slice_max_s: int = 1200  # 每题时间片上限（秒）
    solver_cmd: list[str] = field(default_factory=list)  # solver 入口命令
    vpn_configs: list[str] = field(default_factory=list)


def resolve_config(root: str | None = None, environ: dict[str, str] | None = None) -> RuntimeConfig:
    """本地 / 托管双模解析。缺凭据或 LLM_API_KEY → 硬失败（§6.5 第 6 条）。"""
    env = dict(os.environ if environ is None else environ)
    ingest: IngestResult = resolve_ingest(root, env)

    # 模式判定：有 ovpn 下发 = 本地评测；否则视为托管（无 ing/ 是托管常态）
    mode = "local" if ingest.vpn_configs else "managed"

    gateway_mode = env.get("GATEWAY_MODE", "on" if mode == "managed" else "off").lower()
    llm_base = env.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
    if gateway_mode == "on":
        llm_base = rewrite_to_gateway(llm_base)  # 全项目唯一改写点（§6.2）

    api_key = env.get("LLM_API_KEY") or env.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise ValueError("LLM_API_KEY missing: no model = zero score, refusing to start")

    def _int(name: str, default: int) -> int:
        return int(env[name]) if env.get(name) else default

    def _float(name: str, default: float) -> float:
        return float(env[name]) if env.get(name) else default

    concurrency = _int("CONCURRENCY_LIMIT", 3)
    if concurrency > 3:
        raise ValueError("CONCURRENCY_LIMIT must not exceed platform hard limit 3")

    solver_cmd_env = env.get("SOLVER_CMD", "")
    return RuntimeConfig(
        mode=mode,
        base_url=ingest.base_url.rstrip("/"),
        token=ingest.token,
        llm_base_url=llm_base,
        llm_api_key=api_key,
        llm_model=env.get("LLM_MODEL", "deepseek-chat"),
        total_time_limit_s=_int("TOTAL_TIME_LIMIT", 21600),
        reserve_seconds=_int("RESERVE_SECONDS", 600),
        concurrency_limit=concurrency,
        api_max_qps=_float("API_MAX_QPS", 3.0),
        hint_cost_threshold=_float("HINT_COST_THRESHOLD", 0.1),
        slice_min_s=_int("SLICE_MIN_S", 120),
        slice_max_s=_int("SLICE_MAX_S", 1200),
        solver_cmd=solver_cmd_env.split() if solver_cmd_env else [],
        vpn_configs=ingest.vpn_configs,
    )
