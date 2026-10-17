"""结构化执行状态 —— 从工具输出提取可跨 attempt 复用的事实。

设计约束：纯正则/集合运算，零 LLM；任何失败都不抛出（调用方按增强处理）。
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_ITEMS = 50

_ASSET_RE = re.compile(r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9.-]+\.[a-z]{2,})(?::\d{2,5})?\b")
_URL_RE = re.compile(r"https?://[^\s\"'<>]{4,200}")
_CMD_RE = re.compile(r"^\$\s+(.{1,200})")
_BAD_CRED_RE = re.compile(r"[(){},;$+\"'\s]")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


def _host_port(value: str) -> tuple[str, str] | None:
    text = value.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if text.count(":") != 1:
        return None
    host, port = text.rsplit(":", 1)
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        return None
    return host, port


def _asset_ok(value: str) -> bool:
    """只保留 host:port；JS chunk/字体/CSS 文件名和裸域名不算资产。"""
    parsed = _host_port(value)
    if parsed is None:
        return False
    host, _port = parsed
    if host in _LOOPBACK_HOSTS:
        return False
    if host.startswith(("127.", "169.254.")):
        return False
    if re.match(r"172\.(1[7-9]|2\d|3[01])\.", host):
        return False
    return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host))


def _lead_ok(value: str) -> bool:
    parsed = _host_port(value)
    if parsed is None:
        return False
    host, _port = parsed
    return host not in _LOOPBACK_HOSTS and bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host))


def _cred_ok(value: str) -> bool:
    value = value.strip().rstrip(";,.}])")
    if not 3 <= len(value) <= 120:
        return False
    if value.lower() in {"...", "<...>", "redacted", "password", "passwd", "token", "secret"}:
        return False
    # JS 表达式/字体族/shell 片段不是凭据
    return not _BAD_CRED_RE.search(value)


_CRED_RE = [
    re.compile(
        r"(?i)(?:password|passwd|pwd|secret|token|api[_-]?key)[\"']?\s*[:=]\s*"
        r"[\"']?([^\s\"'<>]{3,120})"
    ),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+([A-Za-z0-9._\-]{10,})"),
    re.compile(r"\b([A-Za-z0-9._%+-]{2,32}):([^\s:@]{3,80})@"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]
_VERSION_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_.-]*/\d+\.\d+(?:\.\d+)?)\b")
_ERROR_RE = re.compile(
    r"(?i)\b(?:401|403|404|500|denied|forbidden|timeout|timed out|refused|error)\b"
)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
        if len(out) >= MAX_ITEMS:
            break
    return out


def extract_findings(text: str) -> dict:
    credentials: list[str] = []
    for pattern in _CRED_RE:
        for match in pattern.finditer(text):
            if match.lastindex == 2 and match.group(0).endswith("@"):
                value = f"{match.group(1)}:{match.group(2)}"
            else:
                value = match.group(1) if match.lastindex else match.group(0)
            cleaned = value.strip().rstrip(";,.}])")
            if _cred_ok(cleaned):
                credentials.append(cleaned)
    endpoints = [match.group(0) for match in _URL_RE.finditer(text) if _lead_ok(match.group(0))]
    endpoints.extend(
        match.group(0) for match in _ASSET_RE.finditer(text.lower()) if _asset_ok(match.group(0))
    )
    versions = [match.group(1) for match in _VERSION_RE.finditer(text)]
    errors = [line.strip()[:160] for line in text.splitlines() if _ERROR_RE.search(line)]
    return {
        "credentials": _dedupe(credentials),
        "endpoints": _dedupe(endpoints),
        "versions": _dedupe(versions),
        "errors": _dedupe(errors),
    }


def synthesize_prior(events: list[dict], candidates: list[str]) -> dict:
    assets: list[str] = []
    explored: list[str] = []
    leads: list[str] = []
    credentials: list[str] = []
    findings: list[str] = []
    for event in events:
        text = str(event.get("text", ""))
        if not text:
            continue
        assets.extend(
            match.group(0)
            for match in _ASSET_RE.finditer(text.lower())
            if _asset_ok(match.group(0))
        )
        leads.extend(match.group(0) for match in _URL_RE.finditer(text) if _lead_ok(match.group(0)))
        for line in text.splitlines():
            match = _CMD_RE.match(line.strip())
            if match:
                explored.append(match.group(1)[:120])
        extracted = extract_findings(text)
        credentials.extend(extracted["credentials"])
        findings.extend(extracted["versions"])
    known = set(candidates)
    leads = [url for url in leads if not any(url in c or c in url for c in known)]
    return {
        "assets": _dedupe(assets),
        "explored": _dedupe(explored),
        "open_leads": _dedupe(leads),
        "credentials": _dedupe(credentials),
        "findings": _dedupe(findings),
    }


def append_findings(path: Path, findings: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if path.is_file():
        existing = set(path.read_text(encoding="utf-8", errors="replace").splitlines())
    new_lines: list[str] = []
    for key in ("credentials", "endpoints", "versions", "errors"):
        for value in findings.get(key) or []:
            line = f"{key}: {value}"
            if line not in existing and line not in new_lines:
                new_lines.append(line)
    if new_lines:
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(new_lines) + "\n")
