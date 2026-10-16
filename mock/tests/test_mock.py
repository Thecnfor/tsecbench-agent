"""mock server 行为矩阵单测（runbook.md §4.1/§4.2）。

每条用例用 make_server 起独立内存 server（端口随机、状态隔离），
用 urllib 直接打 HTTP —— 走的就是编排器将来的真实路径。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest
from mock_server.server import make_server
from mock_server.state import ChallengeSpec

TOKEN = "mock-token"


# ---------------------------------------------------------------- fixtures ----
@pytest.fixture
def factory():
    servers: list = []

    def make(specs=None, env=None):
        server, state = make_server(specs, env=env, token=TOKEN)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}", state

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


def req(base: str, method: str, path: str, body=None, token: str = TOKEN):
    """返回 (status, parsed_json)。非 JSON 响应体时 parsed_json 为 {"_raw": ...}。"""
    headers = {}
    if token is not None:
        headers["BENCHMARK_TOKEN"] = token
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    else:
        status = 200
    try:
        return status, json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return status, {"_raw": raw.decode(errors="replace")}


def req_raw_body(base: str, method: str, path: str, body: bytes, token: str = TOKEN):
    request = urllib.request.Request(
        base + path, method=method, data=body, headers={"BENCHMARK_TOKEN": token}
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return exc.code, {"_raw": raw.decode(errors="replace")}


def wait_until(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------- 12 条用例 ----
def test_01_timeline_pending_then_available(factory):
    """start 后 pending、addr 为空；延迟后才 available、addr 非空（platform-api §4.1）。"""
    base, _ = factory([ChallengeSpec("t1", flags=["FLAG{x}"], start_delay_s=0.5)])
    req(base, "POST", "/openapi/v1/challenges/start?unique_code=t1")
    status, items = req(base, "GET", "/openapi/v1/challenges")
    assert status == 200
    row = next(c for c in items if c["unique_code"] == "t1")
    assert row["container_status"] == "pending"
    assert row["container_addr"] == []  # 非 available 时一律空数组
    assert wait_until(
        lambda: req(base, "GET", "/openapi/v1/challenges")[1][0]["container_status"] == "available"
    )
    row = req(base, "GET", "/openapi/v1/challenges")[1][0]
    assert row["container_addr"] == ["127.0.0.1:9100"]


def test_02_start_409_at_fourth_then_recover(factory):
    """并发上限 3：第 4 题 start → 409 invalid_state；close 释放后可再 start。"""
    specs = [ChallengeSpec(f"c{i}", flags=["FLAG{a}"]) for i in range(4)]
    base, _ = factory(specs)
    for i in range(3):
        assert req(base, "POST", f"/openapi/v1/challenges/start?unique_code=c{i}")[0] == 200
    status, body = req(base, "POST", "/openapi/v1/challenges/start?unique_code=c3")
    assert (status, body["code"]) == (409, "invalid_state")
    assert req(base, "POST", "/openapi/v1/challenges/close?unique_code=c0")[0] == 200
    assert req(base, "POST", "/openapi/v1/challenges/start?unique_code=c3")[0] == 200


def test_03_multi_flag_progressive(factory):
    """多 flag 题逐次递增 correct_flag_count；全中后 is_completed。"""
    base, _ = factory([ChallengeSpec("m1", flags=["FLAG{a}", "FLAG{b}"])])
    req(base, "POST", "/openapi/v1/challenges/start?unique_code=m1")
    s1, r1 = req(
        base, "POST", "/openapi/v1/challenges/submit", {"unique_code": "m1", "flag": "FLAG{a}"}
    )
    assert (s1, r1["matched_flag_index"], r1["correct_flag_count"], r1["total_flag_count"]) == (
        200,
        0,
        1,
        2,
    )
    # 错误 flag：HTTP 200，matched_flag_index 为 null，进度不变
    s2, r2 = req(
        base, "POST", "/openapi/v1/challenges/submit", {"unique_code": "m1", "flag": "FLAG{wrong}"}
    )
    assert (s2, r2["matched_flag_index"], r2["correct_flag_count"]) == (200, None, 1)
    s3, r3 = req(
        base, "POST", "/openapi/v1/challenges/submit", {"unique_code": "m1", "flag": "FLAG{b}"}
    )
    assert (s3, r3["matched_flag_index"], r3["correct_flag_count"]) == (200, 1, 2)
    rows = req(base, "GET", "/openapi/v1/challenges")[1]
    assert next(c for c in rows if c["unique_code"] == "m1")["is_completed"] is True


def test_04_duplicate_409(factory):
    base, _ = factory([ChallengeSpec("d1", flags=["FLAG{dup}"])])
    req(base, "POST", "/openapi/v1/challenges/start?unique_code=d1")
    first, _ = req(
        base, "POST", "/openapi/v1/challenges/submit", {"unique_code": "d1", "flag": "FLAG{dup}"}
    )
    assert first == 200
    status, body = req(
        base, "POST", "/openapi/v1/challenges/submit", {"unique_code": "d1", "flag": "FLAG{dup}"}
    )
    assert (status, body["code"]) == (409, "duplicate")


def test_05_submit_422_framework_shape(factory):
    """flag 长度非法 → 框架级 422（{"detail": [...]}，与业务错误结构不同）。畸形 body 同样 422。"""
    base, _ = factory([ChallengeSpec("l1", flags=["FLAG{a}"])])
    req(base, "POST", "/openapi/v1/challenges/start?unique_code=l1")
    status, body = req(
        base, "POST", "/openapi/v1/challenges/submit", {"unique_code": "l1", "flag": ""}
    )
    assert status == 422 and "detail" in body and "code" not in body
    status, body = req(
        base, "POST", "/openapi/v1/challenges/submit", {"unique_code": "l1", "flag": "x" * 4097}
    )
    assert status == 422 and "detail" in body
    status, body = req_raw_body(base, "POST", "/openapi/v1/challenges/submit", b"not-json")
    assert status == 422 and "detail" in body


def test_06_hint_response_shape_and_completed_409(factory):
    """hint 响应**不带** hint_cost_radio / hint_viewed（本届就没有）；通关后 hint → 409。"""
    base, _ = factory([ChallengeSpec("h1", flags=["FLAG{a}"])])
    req(base, "POST", "/openapi/v1/challenges/start?unique_code=h1")
    status, body = req(base, "GET", "/openapi/v1/challenges/hint?unique_code=h1")
    assert status == 200
    assert set(body) == {"hint"}  # 泄漏任何扣分/已读状态都是错的
    req(base, "POST", "/openapi/v1/challenges/submit", {"unique_code": "h1", "flag": "FLAG{a}"})
    status, body = req(base, "GET", "/openapi/v1/challenges/hint?unique_code=h1")
    assert (status, body["code"]) == (409, "invalid_state")  # 题目级，非任务级


def test_07_task_timeout_invalidates_everything(factory):
    """任务级超时后**所有**接口（含 GET /challenges）一律 409 invalid_state。"""
    base, _ = factory([ChallengeSpec("t7", flags=["FLAG{a}"])], env={"MOCK_TOTAL_LIMIT_S": "0.2"})
    assert req(base, "GET", "/openapi/v1/challenges")[0] == 200
    time.sleep(0.35)
    for method, path, body in [
        ("GET", "/openapi/v1/challenges", None),
        ("POST", "/openapi/v1/challenges/start?unique_code=t7", None),
        ("GET", "/openapi/v1/challenges/hint?unique_code=t7", None),
        ("POST", "/openapi/v1/challenges/submit", {"unique_code": "t7", "flag": "FLAG{a}"}),
        ("POST", "/openapi/v1/challenges/close?unique_code=t7", None),
    ]:
        status, body_ = req(base, method, path, body)
        assert (status, body_.get("code")) == (409, "invalid_state"), (method, path)


def test_08_rate_limit_429(factory):
    """MOCK_RATE_LIMIT=2：第 3 个请求 429；窗口过后恢复。"""
    base, _ = factory([ChallengeSpec("r1", flags=["FLAG{a}"])], env={"MOCK_RATE_LIMIT": "2"})
    assert req(base, "GET", "/openapi/v1/challenges")[0] == 200
    assert req(base, "GET", "/openapi/v1/challenges")[0] == 200
    status, body = req(base, "GET", "/openapi/v1/challenges")
    assert (status, body["code"]) == (429, "rate_limited")
    time.sleep(1.05)
    assert req(base, "GET", "/openapi/v1/challenges")[0] == 200


def test_09_level_gate_403_then_unlock(factory):
    """MOCK_LEVEL_GATE=on：level 2 题全接口 403；正确提交 level 1 题后解锁。"""
    specs = [
        ChallengeSpec("easy", flags=["FLAG{e}"]),
        ChallengeSpec("hard", level=2, flags=["FLAG{h}"]),
    ]
    base, state = factory(specs, env={"MOCK_LEVEL_GATE": "on", "MOCK_CURRENT_LEVEL": "1"})
    status, body = req(base, "POST", "/openapi/v1/challenges/start?unique_code=hard")
    assert (status, body["code"]) == (403, "forbidden")
    # 未解锁题的 hint / submit 同样 403
    assert req(base, "GET", "/openapi/v1/challenges/hint?unique_code=hard")[0] == 403
    assert req(base, "GET", "/openapi/v1/challenges")[0] == 200  # 列题不受限
    # 正确提交 level 1 → 解锁 level 2
    unlocked, _ = req(
        base, "POST", "/openapi/v1/challenges/submit", {"unique_code": "easy", "flag": "FLAG{e}"}
    )
    assert unlocked == 200
    assert req(base, "POST", "/openapi/v1/challenges/start?unique_code=hard")[0] == 200
    assert state.current_level == 2


def test_10_fault_slow_then_503_then_recover(factory):
    """故障注入：slow 阻塞后正常；503 → resource_unavailable；ok 恢复；500 → internal_error。"""
    base, _ = factory(
        [
            ChallengeSpec("f1", flags=["FLAG{a}"], faults={"start": ["slow:0.3", "503", "ok"]}),
            ChallengeSpec("f2", flags=["FLAG{a}"], faults={"hint": ["500"]}),
        ]
    )
    t0 = time.monotonic()
    assert req(base, "POST", "/openapi/v1/challenges/start?unique_code=f1")[0] == 200
    assert time.monotonic() - t0 >= 0.3  # slow 真实阻塞了
    status, body = req(base, "POST", "/openapi/v1/challenges/start?unique_code=f1")
    assert (status, body["code"]) == (503, "resource_unavailable")
    assert req(base, "POST", "/openapi/v1/challenges/start?unique_code=f1")[0] == 200  # ok 恢复
    status, body = req(base, "GET", "/openapi/v1/challenges/hint?unique_code=f2")
    assert (status, body["code"]) == (500, "internal_error")


def test_11_fault_reset_and_garbage(factory):
    """reset → 连接被断（不产生响应）；garbage → 200 但响应体非 JSON。"""
    base, _ = factory(
        [
            ChallengeSpec("f3", flags=["FLAG{a}"], faults={"start": ["reset"]}),
            ChallengeSpec("f4", flags=["FLAG{a}"], faults={"start": ["garbage"]}),
        ]
    )
    with pytest.raises(Exception):  # noqa: B017 - RemoteDisconnected/URLError 视平台而定
        req(base, "POST", "/openapi/v1/challenges/start?unique_code=f3")
    status, body = req(base, "POST", "/openapi/v1/challenges/start?unique_code=f4")
    assert status == 200 and "_raw" in body  # 畸形体：客户端解析必须容错


def test_12_auth_404_task_not_found(factory):
    """token 缺失/错误 → 404 task_not_found（不是 401/403）。"""
    base, _ = factory([ChallengeSpec("a1", flags=["FLAG{a}"])])
    status, body = req(base, "GET", "/openapi/v1/challenges", token=None)
    assert (status, body["code"]) == (404, "task_not_found")
    status, body = req(base, "GET", "/openapi/v1/challenges", token="wrong-token")
    assert (status, body["code"]) == (404, "task_not_found")
