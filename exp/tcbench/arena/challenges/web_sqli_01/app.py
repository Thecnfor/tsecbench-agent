#!/usr/bin/env python3
"""示例靶机：登录页 SQL 注入（stdlib only）。

- /        登录表单
- /login   拼接 SQL 的登录校验：用户名/密码直接 f-string 进 WHERE（故意留注入点）
- /admin   登录后可见 flag（从环境变量 FLAG_0 读取，由 arena 注入）

预期解法：用户名填 `' OR 1=1-- ` 直接以 admin 身份登录。
"""
import html
import os
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

PORT = int(os.environ.get("PORT", "8080"))
FLAG = os.environ.get("FLAG_0") or os.environ.get("FLAG") or "FLAG{missing_env_flag}"

db = sqlite3.connect(":memory:", check_same_thread=False)
db.execute("CREATE TABLE users (name TEXT PRIMARY KEY, pass TEXT)")
db.execute("INSERT INTO users VALUES ('admin', 'n0t_th3_fl4g_but_a_password')")
db.execute("CREATE TABLE meta (k TEXT, v TEXT)")
db.execute("INSERT INTO meta VALUES ('note', 'welcome to the arena demo shop')")

sessions: dict[str, str] = {}

LOGIN_PAGE = """<!doctype html>
<html><head><title>Arena Demo Shop</title></head>
<body>
<h1>Arena Demo Shop — staff login</h1>
<form method="POST" action="/login">
  <p><label>user <input name="username"></label></p>
  <p><label>pass <input name="password" type="password"></label></p>
  <p><button type="submit">login</button></p>
</form>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 安静一点
        print(f"[target] {fmt % args}", flush=True)

    def _send(self, status: int, body: str, content_type: str = "text/html", extra: list | None = None):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for kv in extra or []:
            self.send_header(*kv)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, LOGIN_PAGE)
        elif self.path == "/admin":
            sid = self._sid()
            user = sessions.get(sid)
            if not user:
                self._send(401, "<h1>401 — please login first</h1>")
                return
            self._send(
                200,
                f"<h1>welcome, {html.escape(user)}</h1>"
                f"<p>staff flag: <code>{html.escape(FLAG)}</code></p>",
            )
        else:
            self._send(404, "<h1>404</h1>")

    def do_POST(self):
        if self.path != "/login":
            self._send(404, "<h1>404</h1>")
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode(errors="replace"))
        username = (form.get("username") or [""])[0]
        password = (form.get("password") or [""])[0]
        # 故意的注入点：直接拼接进 SQL
        query = f"SELECT name FROM users WHERE name='{username}' AND pass='{password}'"
        try:
            row = db.execute(query).fetchone()
        except sqlite3.Error as e:
            self._send(500, f"<h1>database error</h1><pre>{html.escape(str(e))}</pre>")
            return
        if row:
            sid = secrets.token_hex(16)
            sessions[sid] = row[0]
            self._send(302, "<h1>redirecting…</h1>", extra=[("Location", "/admin"), ("Set-Cookie", f"sid={sid}; Path=/")])
        else:
            self._send(401, "<h1>401 — invalid credentials</h1><p><a href=/>back</a></p>")

    def _sid(self) -> str:
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid":
                return v
        return ""


if __name__ == "__main__":
    print(f"[target] listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
