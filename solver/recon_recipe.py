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
import shutil
import subprocess
import sys
from urllib.parse import urlparse

TOOL_TIMEOUT = 120


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


def recon_host(target: str) -> dict:
    """主机侦察：nmap top-1000 端口 + 服务版本；nmap 缺失则返回错误标注。"""
    result: dict = {"target": target, "open_ports": [], "services": [], "raw": ""}
    nmap = _which("nmap")
    if not nmap:
        result["raw"] = "[tool-error] nmap not installed"
        return result
    out = _run([nmap, "-Pn", "--top-ports", "1000", "-sV", "--open", target])
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


def recon_web(url: str) -> dict:
    """Web 侦察：响应头 + 标题 + 技术栈线索 + 目录枚举（gobuster/feroxbuster 二选一）。"""
    result: dict = {"url": url, "headers": {}, "title": "", "tech": [], "dirs": [], "raw": ""}
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        result["raw"] = "[input-error] --url must be absolute (http://host[:port])"
        return result

    curl = _which("curl")
    if curl:
        out = _run([curl, "-sS", "-m", "30", "-D", "-", "-o", "-", url], timeout=60)
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
    if dirbust:
        if dirbust.endswith("feroxbuster"):
            argv = [dirbust, "-u", url, "-w", wordlist, "--silent", "-t", "30", "-d", "1"]
        else:
            argv = [dirbust, "dir", "-u", url, "-w", wordlist, "-q", "--no-error", "-t", "30"]
        out = _run(argv, timeout=TOOL_TIMEOUT)
        for line in out.splitlines():
            parts = line.split()
            if parts and (parts[0].isdigit() or parts[0].startswith("/")):
                result["dirs"].append(line.strip()[:160])
        result["dirs"] = result["dirs"][:50]
    else:
        result["dirs"] = ["[tool-error] neither feroxbuster nor gobuster installed"]
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

    args = parser.parse_args()
    report: dict = {"mode": args.mode, "errors": []}
    try:
        if args.mode == "host":
            report["host"] = recon_host(args.target)
        elif args.mode == "web":
            report["web"] = recon_web(args.url)
        else:
            report["host"] = recon_host(args.target)
            url = args.url or (
                f"http://{args.target}:80"
                if 80 in [int(p) for p in report["host"]["open_ports"] if p.isdigit()]
                else None
            )
            if url:
                report["web"] = recon_web(url)
    except Exception as exc:  # 永不崩，JSON 兜底
        report["errors"].append(f"{type(exc).__name__}: {exc}")

    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
