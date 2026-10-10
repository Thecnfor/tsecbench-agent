"""mock server HTTP 层：路由、鉴权、限频、任务超时守卫、关卡门、故障注入。

请求处理顺序（顺序即语义，勿调换）：
  1. 鉴权（BENCHMARK_TOKEN 头）→ 404 task_not_found（不是 401/403，platform-api.md §1）
  2. 限频（MOCK_RATE_LIMIT，上届语义每秒 N 次全接口共享）→ 429
  3. 任务级超时 → 一律 409 invalid_state（platform-api.md §4.10）
  4. 关卡门（MOCK_LEVEL_GATE=on）→ 403（除 GET /challenges 外）
  5. 题目级故障注入 → slow/500/503/reset/garbage
  6. 正常业务
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .state import CONCURRENCY_LIMIT, MockState, build_state

FAIL_LOUD_ERRORS = {
    "500": ("internal_error", "mock injected internal error", 500),
    "503": ("resource_unavailable", "mock injected resource unavailable", 503),
}


class Handler(BaseHTTPRequestHandler):
    state: MockState
    token: str
    level_gate: bool = False

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002 - stdlib 签名
        print(f"[mock] {self.address_string()} {fmt % args}", flush=True)

    # ---- 底层工具 -------------------------------------------------------------
    def _send(self, status: int, payload: dict | list) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: str, message: str, http_status: int) -> None:
        self._send(http_status, {"code": code, "message": message, "detail": {}})

    def _qs_code(self) -> str:
        qs = parse_qs(urlparse(self.path).query)
        return (qs.get("unique_code") or [""])[0]

    # ---- 横切守卫 --------------------------------------------------------------
    def _guards(self) -> bool:
        """返回 True 表示已被守卫拦截（响应已写出）。"""
        if self.headers.get("BENCHMARK_TOKEN") != self.token:
            self._err("task_not_found", "missing or invalid BENCHMARK_TOKEN", 404)
            return True
        if not self.state.check_rate_limit():
            self._err("rate_limited", "too many requests per second", 429)
            return True
        if self.state.task_expired():
            self._err("invalid_state", "task already ended", 409)
            return True
        return False

    def _level_gate(self, unique_code: str) -> bool:
        """关卡门开启时，未解锁关卡的一切题目级操作 → 403。
        GET /challenges 不受限（列题总是允许，编排在列完之后才撞 403）。"""
        if not self.level_gate:
            return False
        entry = self.state.live.get(unique_code)
        if entry is None:
            return False
        if entry["spec"].level > self.state.current_level:
            self._err("forbidden", "level not unlocked", 403)
            return True
        return False

    def _apply_fault(self, unique_code: str, interface: str) -> bool:
        """消费故障注入项。返回 True 表示注入已发生（响应已写出或连接已断）。"""
        fault = self.state.pop_fault(unique_code, interface)
        if fault is None or fault == "ok":
            return False
        if fault.startswith("slow:"):
            time.sleep(float(fault.split(":", 1)[1]))
            return False  # 慢响应后仍正常处理
        if fault == "reset":
            # 不写任何响应直接断连：客户端表现为 RemoteDisconnected / ConnectionReset
            self.close_connection = True
            return True
        if fault == "garbage":
            body = b"<html>mock garbage</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        if fault in FAIL_LOUD_ERRORS:
            self._err(*FAIL_LOUD_ERRORS[fault])
            return True
        return False  # 未知注入项视为无故障

    # ---- 路由 -------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib 命名
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib 命名
        length = int(self.headers.get("Content-Length") or 0)
        self._body = self.rfile.read(length) if length else b""
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        routes = {
            ("GET", "/openapi/v1/challenges"): self._list,
            ("POST", "/openapi/v1/challenges/start"): self._start,
            ("GET", "/openapi/v1/challenges/hint"): self._hint,
            ("POST", "/openapi/v1/challenges/submit"): self._submit,
            ("POST", "/openapi/v1/challenges/close"): self._close,
        }
        handler = routes.get((method, path))
        if handler is None:
            self._send(
                404, {"code": "not_found", "message": f"no route {method} {path}", "detail": {}}
            )
            return
        if self._guards():
            return
        handler()

    # ---- 5 个接口 ----------------------------------------------------------------
    def _list(self) -> None:
        self._send(200, self.state.list_challenges())

    def _start(self) -> None:
        code = self._qs_code()
        with self.state.lock:
            entry = self.state.live.get(code)
            if entry is None:
                self._err("challenge_not_found", f"unknown unique_code {code!r}", 404)
                return
            if self._level_gate(code):
                return
            if self._apply_fault(code, "start"):
                return
            if entry["container_status"] == "stopped":
                if self.state.active_count() >= CONCURRENCY_LIMIT:
                    self._err("invalid_state", "active container limit reached", 409)
                    return
                entry["container_status"] = "pending"
                entry["pending_since"] = time.monotonic()
                entry["container_addr"] = []
            # 已 pending/available 的重复 start 幂等返回当前状态（文档未定义，选幂等）
            view = self.state.challenge_view(entry)
        self._send(
            200,
            {
                "unique_code": code,
                "container_status": view["container_status"],
                "container_addr": view["container_addr"],
            },
        )

    def _hint(self) -> None:
        code = self._qs_code()
        with self.state.lock:
            entry = self.state.live.get(code)
            if entry is None:
                self._err("challenge_not_found", "unknown unique_code", 404)
                return
            if self._level_gate(code):
                return
            if self._apply_fault(code, "hint"):
                return
            view = self.state.challenge_view(entry)
            if view["is_completed"]:
                # 题目级 409：已通关题禁止取 hint（platform-api.md §4.5/§4.7）
                self._err("invalid_state", "challenge already completed", 409)
                return
            self.state.hint_viewed.add(code)
            # 故意不带 hint_cost_radio / hint_viewed —— 本届文档就没有（runbook.md §4.1）
            self._send(200, {"hint": f"mock hint for {code}"})

    def _submit(self) -> None:
        try:
            payload = json.loads(self._body or b"")
            flag = str(payload.get("flag", ""))
            code = str(payload.get("unique_code", ""))
        except json.JSONDecodeError:
            # FastAPI 对畸形 body 返回框架级 422
            self._send(422, {"detail": [{"msg": "JSON decode error"}]})
            return
        with self.state.lock:
            entry = self.state.live.get(code)
            if entry is None:
                self._err("challenge_not_found", f"unknown unique_code {code!r}", 404)
                return
            if self._level_gate(code):
                return
            if self._apply_fault(code, "submit"):
                return
            if not 1 <= len(flag) <= 4096:
                # 框架级 422：响应体结构与业务异常完全不同（platform-api.md §4.9）
                self._send(422, {"detail": [{"msg": "flag length must be within 1-4096"}]})
                return
            spec = entry["spec"]
            total = len(spec.flags)
            correct = sum(1 for f in spec.flags if (code, f) in self.state.correct_submitted)
            if flag in spec.flags:
                if (code, flag) in self.state.correct_submitted:
                    self._err("duplicate", "flag already submitted", 409)
                    return
                self.state.correct_submitted.add((code, flag))
                self.state.submitted_any.add((code, flag))
                # 解锁信号：正确提交后 current_level 越过该题（上届语义，runbook.md §4.1）
                self.state.current_level = max(self.state.current_level, spec.level + 1)
                self._send(
                    200,
                    {
                        "matched_flag_index": spec.flags.index(flag),
                        "correct_flag_count": correct + 1,
                        "total_flag_count": total,
                    },
                )
                return
            self.state.submitted_any.add((code, flag))
            # 错误 flag：HTTP 200，matched_flag_index 为 null（进度真值在 correct_flag_count）
            self._send(
                200,
                {
                    "matched_flag_index": None,
                    "correct_flag_count": correct,
                    "total_flag_count": total,
                },
            )

    def _close(self) -> None:
        code = self._qs_code()
        with self.state.lock:
            entry = self.state.live.get(code)
            if entry is None:
                self._err("challenge_not_found", "unknown unique_code", 404)
                return
            if self._level_gate(code):
                return
            if self._apply_fault(code, "close"):
                return
            entry["container_status"] = "stopped"
            entry["pending_since"] = None
            entry["container_addr"] = []
            self._send(200, {"unique_code": code, "container_status": "stopped"})


def make_server(
    specs=None,
    *,
    token: str = "mock-token",
    host: str = "127.0.0.1",
    port: int = 0,
    env: dict[str, str] | None = None,
) -> tuple[ThreadingHTTPServer, MockState]:
    """构造独立 server（port=0 由系统分配）。测试用，与 __main__ 共用全部逻辑。"""
    env = env or {}
    state = build_state(specs, env=env)

    class BoundHandler(Handler):
        pass

    BoundHandler.state = state
    BoundHandler.token = token
    BoundHandler.level_gate = env.get("MOCK_LEVEL_GATE", "") == "on"
    server = ThreadingHTTPServer((host, port), BoundHandler)
    return server, state


def main() -> int:
    token = os.environ.get("MOCK_TOKEN", "mock-token")
    state = build_state()
    server = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("MOCK_PORT", "8787"))), Handler)
    Handler.state = state
    Handler.token = token
    Handler.level_gate = os.environ.get("MOCK_LEVEL_GATE", "") == "on"
    host, port = server.server_address[:2]
    print(
        f"[mock] listening on http://{host}:{port}"
        f" (token={token[:4]}***, level_gate={Handler.level_gate},"
        f" rate_limit={state.rate_limit}, total_limit_s={state.total_limit_s})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
