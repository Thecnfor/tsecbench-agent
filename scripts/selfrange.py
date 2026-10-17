#!/usr/bin/env python3
"""自建靶场 —— 4 个真实本地靶标，与 mock_server 的内置题面/真值对齐。

mock 平台把第 idx 题的 container_addr 定为 127.0.0.1:(9100+idx)（按 unique_code
排序）。本脚本在 9100..9103 提供真有漏洞的 HTTP 服务，flag 与 mock 真值一致：

  mock_web_01   :9100  登录页 SQL 注入（username 含 ' 即回显 flag）
  mock_pwn_01   :9101  命令执行接口（cmd= 参数直接回显 + 双 flag）
  mock_delay_01 :9102  普通页面，源码注释里藏 flag
  mock_hard_01  :9103  admin 面板（Cookie role=admin 才给 flag）

用法：uv run python scripts/selfrange.py [--port-base 9100]
配合：MOCK_TOKEN=mock-token uv run python -m mock_server
     然后以 BENCHMARK_BASE_URL=http://127.0.0.1:8787 起 orchestrator。
"""

from __future__ import annotations

import argparse
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

FLAGS = {
    "web": "FLAG{mock_web_flag_a}",
    "pwn_a": "FLAG{mock_pwn_flag_a}",
    "pwn_b": "FLAG{mock_pwn_flag_b}",
    "delay": "FLAG{mock_delay_flag}",
    "hard": "FLAG{mock_hard_flag}",
}


def make_handler(kind: str):
    class H(BaseHTTPRequestHandler):
        def _r(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if kind == "web":
                if u.path == "/login":
                    user = (q.get("user") or [""])[0]
                    if "'" in user or " or " in user.lower():
                        self._r(200, f"<html>Welcome admin! session={FLAGS['web']}</html>")
                    else:
                        self._r(
                            200,
                            "<html><form action=/login>user<input name=user></form>"
                            "<!-- TODO: parameterized queries --></html>",
                        )
                elif u.path == "/":
                    self._r(200, "<html><a href=/login>login</a></html>")
                else:
                    self._r(404, "not found")
            elif kind == "pwn":
                if u.path == "/run":
                    self._r(
                        200,
                        f"uid=1000(agent) gid=1000\n{FLAGS['pwn_a']}\n{FLAGS['pwn_b']}\n",
                        "text/plain",
                    )
                elif u.path == "/download":
                    self._r(200, "ELF...", "application/octet-stream")
                else:
                    self._r(200, "<html>service: /run?cmd= /download</html>")
            elif kind == "delay":
                self._r(200, f"<html>status ok<!-- backup: {FLAGS['delay']} --></html>")
            elif kind == "hard":
                role = self.headers.get("Cookie", "")
                if "role=admin" in role:
                    self._r(200, f"<html>admin panel {FLAGS['hard']}</html>")
                else:
                    self._r(403, "<html>forbidden: admin only</html>")
            else:
                self._r(200, "ok")

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(length).decode(errors="replace") if length else ""
            if kind == "web" and "user=" in body and ("'" in body or "or " in body.lower()):
                self._r(200, f"<html>Welcome admin! {FLAGS['web']}</html>")
            else:
                self._r(200, "<html>ok</html>")

        def log_message(self, *a) -> None:
            pass

    return H


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-base", type=int, default=9100)
    args = ap.parse_args()
    # 与 mock_state 的 sorted(unique_code) 顺序一致：
    # delay→9100, hard→9101, pwn→9102, web→9103
    kinds = ["delay", "hard", "pwn", "web"]
    servers = []
    for i, kind in enumerate(kinds):
        port = args.port_base + i
        srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(kind))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        print(f"self-range: {kind:5s} http://127.0.0.1:{port}/")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
