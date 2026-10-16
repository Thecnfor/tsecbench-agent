"""Platform API Client —— 平台 API 的唯一出口（architecture.md §3/§5.1）。

4 个接口（platform-api.md §2，**不含 hint**）+ 全局令牌桶 + 重试 + 错误码分派。

hint 接口被主动删除（见 §4.4）：扣分不可逆且本届读不到 `hint_cost_radio`，
实测 10 次取 hint 只有 1 次换来解出 —— 期望收益为负，整个调用面从代码里拿掉，
不给「随手取一下」留任何入口。

错误分派按**接口**而非按错误码（platform-api.md §4.5 三义性）：
  - 404 task_not_found                     → TaskEnded（致命，全局停机）
  - 404 challenge_not_found                → ChallengeGone（剔除该题）
  - 409 invalid_state @ start              → StartFull（腾位置重试）
  - 409 invalid_state @ 其他               → TaskEnded（任务结束，全局停机）
  - 409 duplicate @ submit                 → Duplicate（不算失败）
  - 403（本届未列出，防御性）              → LevelLocked（移入 LOCKED）
  - 429 / 500 / 网络错误                   → 内部退避重试，耗尽后 RetryableError
  - 503 resource_unavailable               → 退避 5/15/45 jitter，耗尽后 ResourceUnavailable
  - 422（框架格式 {"detail":[...]}）       → FlagRejected（过滤丢弃）

限速是本模块的一部分，不是可选项（§5.1 末段）。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import quote_plus

from .ratelimit import TokenBucket

DEFAULT_TIMEOUT_S = 10.0
BACKOFF_503 = (5.0, 15.0, 45.0)


# ---------------------------------------------------------------- 异常体系 ----
class ApiError(Exception):
    """平台 API 错误基类。带 HTTP 状态与响应体摘要，便于审计日志。"""


@dataclass(slots=True)
class ApiErrorMeta:
    status: int | None = None
    code: str | None = None
    message: str = ""


class TaskEnded(ApiError):
    """404 task_not_found 或任务级 409 —— 全局停机。"""


class ChallengeGone(ApiError):
    """404 challenge_not_found —— 从 Registry 剔除。"""


class StartFull(ApiError):
    """409 invalid_state @ start —— 活跃数达上限，腾位置重试。"""


class Duplicate(ApiError):
    """409 duplicate @ submit —— 不算失败，标记已入袋。"""


class LevelLocked(ApiError):
    """403 —— 推定关卡未解锁，移入 LOCKED（防御性分支）。"""


class FlagRejected(ApiError):
    """422 —— 候选格式非法，过滤丢弃（响应体是框架格式）。"""


class ResourceUnavailable(ApiError):
    """503 —— 指数退避耗尽后抛出，该题排队尾。"""


class RetryableError(ApiError):
    """429/500/网络错误重试耗尽。"""


# ---------------------------------------------------------------- 客户端 ----
@dataclass(slots=True)
class PlatformClient:
    """async 客户端：令牌桶在事件循环里取票，HTTP 阻塞调用丢进线程池。"""

    base_url: str
    token: str
    qps: float = 3.0
    timeout_s: float = DEFAULT_TIMEOUT_S
    # 重试参数可注入，测试里传极小值避免真实睡眠
    retries_429: int = 2
    retries_500: int = 2
    retries_network: int = 2
    # 单请求重试总时长预算（实测 100 次上界可卡 16 分钟拖死 pipeline）
    request_budget_s: float = 60.0
    delay_429: float = 1.0
    delay_500: float = 1.0
    delay_network: float = 0.5
    backoff_503: tuple[float, ...] = BACKOFF_503
    jitter: float = 0.2
    _bucket: TokenBucket = field(init=False, default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._bucket = TokenBucket(self.qps, self.qps)

    # ---- 底层请求 -------------------------------------------------------------
    def _http(
        self, method: str, path: str, *, query: str = "", body: dict | None = None
    ) -> tuple[int, bytes]:
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        data = json.dumps(body).encode() if body is not None else None
        headers = {"BENCHMARK_TOKEN": self.token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    async def _request(
        self, method: str, path: str, *, query: str = "", body: dict | None = None
    ) -> object:
        """带限速与重试的请求。成功返回解析后的 JSON；解析失败按重试处理。"""
        await self._bucket.acquire()
        attempts_503 = 0
        deadline = time.monotonic() + self.request_budget_s
        for attempt in range(100):  # 上界只是防呆，实际由总时长预算封顶
            if time.monotonic() > deadline:
                raise RetryableError(
                    f"request budget {self.request_budget_s:.0f}s exhausted: {method} {path}"
                )
            try:
                # wait_for 兜底：getaddrinfo（DNS）不受 urlopen timeout 管辖，
                # VPN DNS 抽风时会无限阻塞 executor 线程楔死 pipeline（实测 04:00）。
                status, raw = await asyncio.wait_for(
                    asyncio.to_thread(self._http, method, path, query=query, body=body),
                    timeout=self.request_budget_s + 30.0,
                )
            except (OSError, urllib.error.URLError) as exc:
                # 网络错误 / 连接重置 / 超时（platform-api.md §5.1 末行）
                if attempt < self.retries_network:
                    await self._sleep(self.delay_network)
                    continue
                raise RetryableError(f"network error after retries: {type(exc).__name__}") from exc
            text = raw.decode("utf-8", errors="replace")

            if status == 200:
                try:
                    return json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    # 畸形响应体（mock garbage 注入）视为瞬时故障
                    await self._sleep(self.delay_network)
                    continue

            payload = self._parse_error_body(text)
            code = payload.get("code") if isinstance(payload, dict) else None

            if status == 404:
                if code == "task_not_found":
                    raise TaskEnded("token invalid or task gone (404 task_not_found)")
                raise ChallengeGone(f"404 challenge_not_found: {payload}")
            if status == 403:
                raise LevelLocked(f"403 (level not unlocked, defensive branch): {payload}")
            if status == 409:
                if code == "duplicate":
                    raise Duplicate(f"409 duplicate (not a failure): {payload}")
                if code == "invalid_state":
                    # 三义性按接口分派（platform-api.md §4.5）
                    if method == "POST" and path.endswith("/start"):
                        raise StartFull("409 invalid_state @ start: active limit reached")
                    raise TaskEnded(f"409 invalid_state (task ended): {payload}")
                raise RetryableError(f"409 unexpected code {code!r}")
            if status == 429:
                if attempt < self.retries_429:
                    await self._sleep(self.delay_429)
                    continue
                raise RetryableError("429 rate limited, retries exhausted")
            if status == 503:
                if attempts_503 < len(self.backoff_503):
                    await self._sleep(self.backoff_503[attempts_503])
                    attempts_503 += 1
                    continue
                raise ResourceUnavailable(f"503 resource_unavailable after {attempts_503} backoffs")
            if status == 500:
                if attempt < self.retries_500:
                    await self._sleep(self.delay_500)
                    continue
                raise RetryableError("500 internal_error, retries exhausted")
            if status == 422:
                raise FlagRejected(f"422 framework validation: {payload}")

            # 未知状态码：按可重试处理（宁可多试不可误判任务终止）
            if attempt < self.retries_network:
                await self._sleep(self.delay_network)
                continue
            raise RetryableError(f"unexpected status {status}: {text[:200]}")
        raise RetryableError("unreachable")  # pragma: no cover

    @staticmethod
    def _parse_error_body(text: str) -> object:
        """兼容两种响应体结构：业务 {code,message,detail} 与框架 {"detail":[...]}。"""
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"code": None, "message": text[:200]}

    async def _sleep(self, base: float) -> None:
        await asyncio.sleep(base + random.uniform(0, self.jitter))  # noqa: S311 - 仅抖动

    # ---- 4 个接口（platform-api.md §2）---------------------------------------
    async def list_challenges(self) -> list[dict]:
        data = await self._request("GET", "/openapi/v1/challenges")
        return data  # type: ignore[no-any-return]

    async def start(self, unique_code: str) -> dict:
        data = await self._request(
            "POST", "/openapi/v1/challenges/start", query=f"unique_code={quote_plus(unique_code)}"
        )
        return data  # type: ignore[return-value]

    async def submit(self, unique_code: str, flag: str) -> dict:
        data = await self._request(
            "POST", "/openapi/v1/challenges/submit", body={"unique_code": unique_code, "flag": flag}
        )
        return data  # type: ignore[return-value]

    async def close(self, unique_code: str) -> dict:
        data = await self._request(
            "POST", "/openapi/v1/challenges/close", query=f"unique_code={unique_code}"
        )
        return data  # type: ignore[return-value]
