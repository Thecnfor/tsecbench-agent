"""平台 API 客户端：选题/start/close/查询。控制器用它做硬编排，模型不参与平台流程决策。"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from config import CFG

DIFF_RANK = {"easy": 0, "medium": 1, "hard": 2}


class PlatformError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class TaskFinishedError(PlatformError):
    """平台任务已结束（评测截止）——终态，应立即停止整个 agent，不重试。"""


def api(path: str, method: str = "GET", timeout: float = 20.0, retries: int = 3):
    """返回解析后的 JSON；平台业务错误抛 PlatformError。
    网络类错误（超时/DNS/连接）有限重试，避免调度层被瞬时故障打断。"""
    url = f"{CFG.benchmark_base_url.rstrip('/')}{path}"
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, method=method,
                                     headers={"BENCHMARK_TOKEN": CFG.benchmark_token})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                err = json.loads(body)
                code = err.get("code", str(e.code))
                msg = err.get("message", body)
                # invalid_state + "finished" = 评测截止（终态）→ 专用异常，上层立即停止
                if code == "invalid_state" and "finished" in msg.lower():
                    raise TaskFinishedError(code, msg, e.code) from None
                raise PlatformError(code, msg, e.code) from None
            except json.JSONDecodeError:
                raise PlatformError(f"http_{e.code}", body, e.code) from None
        except (TimeoutError, urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            break
    raise ConnectionError(f"{method} {path} failed after {retries} attempts: "
                          f"{type(last_err).__name__}: {last_err}") from last_err


def fetch_challenges() -> list:
    return api("/openapi/v1/challenges")


def start_challenge(code: str) -> dict:
    return api(f"/openapi/v1/challenges/start?unique_code={code}", method="POST")


def close_challenge(code: str) -> dict:
    return api(f"/openapi/v1/challenges/close?unique_code={code}", method="POST")


def challenge_state(items: list, code: str) -> dict | None:
    for c in items:
        if c["unique_code"] == code:
            return c
    return None


def choose_next(items: list) -> dict | None:
    """未通关题目中选下一题：由易到难（difficulty → level → 题号），可通关题优先。"""
    pending = [c for c in items if not c["is_completed"]]
    if not pending:
        return None
    pending.sort(key=lambda c: (
        0 if c["container_status"] == "available" else 1,  # 已启动的优先复用（续跑/10min接入）
        DIFF_RANK.get(c["difficulty"], 9),
        c.get("level", 9),
        c["unique_code"],
    ))
    return pending[0]


def release_completed(items: list) -> list:
    """通关但容器仍 available 的题全部 close，释放活跃名额。返回被 close 的 code 列表。"""
    closed = []
    for c in items:
        if c["is_completed"] and c["container_status"] == "available":
            try:
                close_challenge(c["unique_code"])
                closed.append(c["unique_code"])
            except PlatformError:
                pass
    return closed


def active_count(items: list) -> int:
    return sum(1 for c in items if c["container_status"] == "available")
