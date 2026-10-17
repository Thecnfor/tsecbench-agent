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
                credentials.append(f"{match.group(1)}:{match.group(2)}")
            else:
                credentials.append(match.group(1) if match.lastindex else match.group(0))
    endpoints = [match.group(0) for match in _URL_RE.finditer(text)]
    endpoints.extend(match.group(0) for match in _ASSET_RE.finditer(text.lower()))
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
        assets.extend(match.group(0) for match in _ASSET_RE.finditer(text.lower()))
        leads.extend(match.group(0) for match in _URL_RE.finditer(text))
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
