#!/usr/bin/env python3
"""本地模拟平台 LLM 网关（*.tsecbench.gw）—— 托管模式仿真专用。

镜像里 GATEWAY_MODE=on 时会把 `https://api.deepseek.com:8080/v1` 改写成
`http://api.deepseek.com.tsecbench.gw:8080/v1`。本进程监听该端口并原样转发
到真实上游，用来在不接平台网关的情况下验证改写与调用链。不记录 Authorization。

用法：GATEWAY_PORT=8080 python3 scripts/mock_gateway.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("GATEWAY_UPSTREAM", "https://api.deepseek.com").rstrip("/")
PORT = int(os.environ.get("GATEWAY_PORT", "8080"))
BIND = os.environ.get("GATEWAY_BIND", "127.0.0.1")
_FORWARD_HEADERS = ("authorization", "content-type", "accept")


class Handler(BaseHTTPRequestHandler):
    def _proxy(self) -> None:  # noqa: ANN202
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() in _FORWARD_HEADERS}
        req = urllib.request.Request(
            UPSTREAM + self.path, data=body, headers=headers, method=self.command
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = resp.read()
                status = resp.status
                ctype = resp.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as exc:
            data, status, ctype = exc.read(), exc.code, "application/json"
        except Exception as exc:  # noqa: BLE001 - 网关只做透传，失败回 502
            data = json.dumps({"error": {"message": f"gateway upstream error: {exc}"}}).encode()
            status, ctype = 502, "application/json"
        print(f"{self.command} {self.path} -> {status} ({len(data)}B)", file=sys.stderr, flush=True)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = _proxy
    do_GET = _proxy

    def log_message(self, *args) -> None:  # noqa: ANN002 - 静默默认访问日志
        return


def main() -> int:
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"[mock-gateway] listening {BIND}:{PORT} -> {UPSTREAM}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
