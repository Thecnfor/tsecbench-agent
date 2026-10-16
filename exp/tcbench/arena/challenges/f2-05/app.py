#!/usr/bin/env python3
"""f2-05 靶机：复刻原平台服务形态（stdlib only）。

- GET /          → web/index.html（题目说明页）
- GET /download  → web/validator（ELF，application/octet-stream 附件下载）
- 其余            → 404
"""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "8080"))
WEB = Path(__file__).resolve().parent / "web"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静一点
        print(f"[target] {fmt % args}", flush=True)

    def _send(self, status: int, body: bytes, content_type: str, extra: list | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for kv in extra or []:
            self.send_header(*kv)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/download":
            blob = WEB / "validator"
            if not blob.is_file():
                # validator 不入库（gitignore），缺失时优雅降级（取法见本题包 README.md）
                self._send(404, b"<h1>404 — validator not deployed</h1>", "text/html")
                return
            self._send(
                200,
                blob.read_bytes(),
                "application/octet-stream",
                [("Content-Disposition", 'attachment; filename="validator"')],
            )
        else:
            self._send(404, b"<h1>404</h1>", "text/html")


if __name__ == "__main__":
    print(f"[target] f2-05 listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
