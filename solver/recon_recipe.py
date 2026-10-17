#!/usr/bin/env python3
"""确定性侦察 recipe —— L2 工具编排 + 结构化预聚合（architecture.md §1.6 杠杆：
单次调用信息密度）。双内核共用：main.py / dual_main.py / pi 扩展（pi_ext/recon.ts）
都调本 CLI，输出恒为 JSON（stdout），工具缺失降级不报错。

用法：
  python3 recon_recipe.py host  --target 10.0.0.1
  python3 recon_recipe.py web   --url http://10.0.0.1:8080
  python3 recon_recipe.py all   --target 10.0.0.1 [--url http://10.0.0.1]

设计约束：
  - 每个外部工具调用带超时，任何失败只记入 errors，不影响整体 JSON；
  - 输出预聚合（端口表 + HTTP 头 + 标题 + 目录命中），绝不回吐原始大段输出；
  - 只做被动/常规侦察（top 端口 + HTTP 头 + 目录枚举），不做破坏性动作。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

TOOL_TIMEOUT = 120
# 目录爆破硬上限：对 Next.js/软 404 站点，common.txt 全量扫能把整片烧光
# （实测 c-03：两次 gobuster 共 ~115s，零产出）。
DIRBUST_TIMEOUT = 25

_PREFERRED_PORTS = ("443", "8443", "80", "8080", "8000", "3000", "7860", "5000")


def resolve_budget_seconds(explicit: float | None = None) -> float | None:
    """显式预算 > SOLVER_DEADLINE_EPOCH - now；两者皆无返回 None（保持旧行为）。"""
    if explicit is not None:
        return max(1.0, float(explicit))
    raw = os.environ.get("SOLVER_DEADLINE_EPOCH", "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw) - time.time())
    except ValueError:
        return None


def derive_web_url(target: str, open_ports: list[str]) -> str:
    """从 nmap 开放端口推导最可能的 Web 入口。"""
    ports = {str(port) for port in open_ports}
    for port in _PREFERRED_PORTS:
        if port not in ports:
            continue
        scheme = "https" if port in ("443", "8443") else "http"
        if (scheme == "https" and port == "443") or (scheme == "http" and port == "80"):
            return f"{scheme}://{target}"
        return f"{scheme}://{target}:{port}"
    return f"http://{target}"


def parse_dirbust_lines(tool: str, out: str, limit: int = 50) -> list[str]:
    """归一化 gobuster / feroxbuster 输出（feroxbuster --silent 只打印完整 URL）。"""
    lines: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        first = parts[0]
        if first.startswith(("http://", "https://", "/")) or first.isdigit():
            lines.append(line.strip()[:160])
        if len(lines) >= limit:
            break
    return lines


def _kb_hits(*parts: str, top_k: int = 2) -> list[dict]:
    query = " ".join(part for part in parts if part).strip()
    if not query:
        return []
    try:
        from kb.index import Index

        return Index().query(query, top_k=top_k)
    except Exception:  # noqa: BLE001 - KB 是增强，不是必需路径
        return []


def _run(argv: list[str], timeout: int = TOOL_TIMEOUT) -> str:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=timeout
        )
        return proc.stdout + (f"\n[stderr]\n{proc.stderr}" if proc.stderr.strip() else "")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"[tool-error] {type(exc).__name__}: {exc}"


def _which(name: str) -> str | None:
    return shutil.which(name)


def recon_host(target: str, budget_s: float | None = None) -> dict:
    """主机侦察：nmap top-1000 端口 + 服务版本；nmap 缺失则返回错误标注。"""
    result: dict = {"target": target, "open_ports": [], "services": [], "raw": ""}
    nmap = _which("nmap")
    if not nmap:
        result["raw"] = "[tool-error] nmap not installed"
        return result
    budget = resolve_budget_seconds(budget_s)
    ports = "1000"
    timeout = TOOL_TIMEOUT
    if budget is not None:
        if budget < 45:
            ports = "100"
        timeout = max(10, min(TOOL_TIMEOUT, int(budget * 0.6)))
    out = _run([nmap, "-Pn", "--top-ports", ports, "-sV", "--open", target], timeout=timeout)
    result["raw"] = out[-4000:]
    for line in out.splitlines():
        # 典型行：80/tcp   open  http    nginx 1.18.0
        parts = line.split()
        if len(parts) >= 4 and parts[0].endswith(("/tcp", "/udp")) and parts[1] == "open":
            port_proto = parts[0]
            port = port_proto.split("/")[0]
            result["open_ports"].append(port)
            result["services"].append(
                {"port": port, "service": parts[2], "version": " ".join(parts[3:])[:80]}
            )
    result["open_ports"] = sorted(set(result["open_ports"]), key=int)
    return result


def _curl_page(curl: str, url: str) -> str:
    """抓页面；502/503/连接类失败自动重试一次（后端冷启动 ≠ 死）。"""
    argv = [curl, "-sS", "--compressed", "-m", "20", "-D", "-", "-o", "-", url]
    out = _run(argv, timeout=30)
    transient = (
        not out.strip()
        or "[tool-error]" in out
        or "502 Bad Gateway" in out
        or "503 Service Unavailable" in out
        or "Connection reset" in out
    )
    if transient:
        time.sleep(5)
        out = _run(argv, timeout=30)
    return out


def recon_web(url: str, budget_s: float | None = None) -> dict:
    """Web 侦察：响应头 + 标题 + 技术栈线索 + 目录枚举（gobuster/feroxbuster 二选一）。"""
    result: dict = {"url": url, "headers": {}, "title": "", "tech": [], "dirs": [], "raw": ""}
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        result["raw"] = "[input-error] --url must be absolute (http://host[:port])"
        return result

    curl = _which("curl")
    if curl:
        out = _curl_page(curl, url)
        head, _, body = out.partition("\r\n\r\n")
        if not body:
            head, _, body = out.partition("\n\n")
        for line in head.splitlines():
            if ":" in line and not line.startswith("HTTP/"):
                k, _, v = line.partition(":")
                result["headers"][k.strip().lower()] = v.strip()[:200]
        for marker in ("<title>", "<TITLE>"):
            if marker in body:
                result["title"] = body.split(marker, 1)[1].split("</", 1)[0][:120]
                break
        # 技术栈指纹（响应头 + body 关键词，轻量）
        haystack = (head + body[:5000]).lower()
        for tech, sig in (
            ("nginx", "nginx"),
            ("apache", "apache"),
            ("express", "x-powered-by: express"),
            ("flask", "werkzeug"),
            ("django", "csrfmiddlewaretoken"),
            ("php", "php"),
            ("tomcat", "tomcat"),
            ("spring", "spring"),
            ("react", "__next"),
        ):
            if sig in haystack:
                result["tech"].append(tech)
        result["raw"] = body[:2000]

    dirbust = _which("feroxbuster") or _which("gobuster")
    wordlist = "/usr/share/dirb/wordlists/common.txt"
    budget = resolve_budget_seconds(budget_s)
    if dirbust and budget is not None and budget < 20:
        result["dirs"] = []
        result["dirbust_skipped"] = "low-budget"
        dirbust = None
    if dirbust:
        if dirbust.endswith("feroxbuster"):
            argv = [
                dirbust,
                "-u",
                url,
                "-w",
                wordlist,
                "--silent",
                "-t",
                "30",
                "-d",
                "1",
                "--time-limit",
                "20s",
                "--filter-status",
                "404",
            ]
        else:
            argv = [
                dirbust,
                "dir",
                "-u",
                url,
                "-w",
                wordlist,
                "-q",
                "--no-error",
                "-t",
                "30",
                "-b",
                "404",
                "--timeout",
                "3s",
            ]
        out = _run(argv, timeout=DIRBUST_TIMEOUT)
        result["dirs"] = parse_dirbust_lines(dirbust, out)
    else:
        result["dirs"] = ["[tool-error] neither feroxbuster nor gobuster installed"]
    hits = _kb_hits(
        result.get("title", ""),
        " ".join(result.get("tech") or []),
        result.get("headers", {}).get("server", ""),
    )
    if hits:
        result["kb_hits"] = [
            {"doc": hit["doc"], "section": hit["section"], "body": hit["body"][:800]}
            for hit in hits
        ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="deterministic recon recipe (JSON out)")
    sub = parser.add_subparsers(dest="mode", required=True)
    p_host = sub.add_parser("host")
    p_host.add_argument("--target", required=True)
    p_web = sub.add_parser("web")
    p_web.add_argument("--url", required=True)
    p_all = sub.add_parser("all")
    p_all.add_argument("--target", required=True)
    p_all.add_argument("--url")
    for sub_parser in (p_host, p_web, p_all):
        sub_parser.add_argument("--budget-seconds", type=float, default=None)

    args = parser.parse_args()
    report: dict = {"mode": args.mode, "errors": []}
    try:
        if args.mode == "host":
            report["host"] = recon_host(args.target, budget_s=args.budget_seconds)
        elif args.mode == "web":
            report["web"] = recon_web(args.url, budget_s=args.budget_seconds)
        else:
            host = recon_host(args.target, budget_s=args.budget_seconds)
            report["host"] = host
            url = args.url or derive_web_url(args.target, host.get("open_ports") or [])
            report["web"] = recon_web(url, budget_s=args.budget_seconds)
    except Exception as exc:  # 永不崩，JSON 兜底
        report["errors"].append(f"{type(exc).__name__}: {exc}")

    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
