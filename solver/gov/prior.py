"""Prior 合成 —— 从 session 事件流 + 候选确定性生成 out/prior.json。

编排器 Harvester 对 prior.json 容忍缺失/畸形（§2.3 不信任 solver 自述），
本模块的价值是把「本轮做过什么」结构化回灌给下一段 attempt：
  assets      —— 事件里出现过的 host:port / URL（客观资产）
  explored    —— 已执行的命令首词序列（防下轮重复侦察）
  open_leads  —— 未验证的线索（输出中出现但未产出 flag 的 URL/版本号）
全程正则与集合运算，零 LLM。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ASSET_RE = re.compile(r"\b(?:\d{1,3}(?:\.\d{1,3}){3}|[a-z0-9.-]+\.[a-z]{2,})(?::\d{2,5})?\b")
_URL_RE = re.compile(r"https?://[^\s\"'<>]{4,200}")
_CMD_RE = re.compile(r"^\$\s+(.{1,200})")

MAX_ITEMS = 50


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
        if len(out) >= MAX_ITEMS:
            break
    return out


def synthesize(events: list[dict], candidates: list[str]) -> dict:
    assets: list[str] = []
    explored: list[str] = []
    leads: list[str] = []
    for ev in events:
        text = str(ev.get("text", ""))
        if not text:
            continue
        assets.extend(m.group(0) for m in _ASSET_RE.finditer(text.lower()))
        leads.extend(m.group(0) for m in _URL_RE.finditer(text))
        for line in text.splitlines():
            m = _CMD_RE.match(line.strip())
            if m:
                explored.append(m.group(1)[:120])
    known = set(candidates)
    # 已在候选里出现过（即已解决线索）的 URL 不再算 open lead
    leads = [u for u in leads if not any(u in c or c in u for c in known)]
    return {
        "assets": _dedupe(assets),
        "explored": _dedupe(explored),
        "open_leads": _dedupe(leads),
    }


def write(out_dir: Path, events: list[dict], candidates: list[str]) -> Path | None:
    """写 out/prior.json；失败返回 None，绝不抛出（harvester 本就容忍缺失）。"""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "prior.json"
        path.write_text(
            json.dumps(synthesize(events, candidates), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
    except OSError:
        return None
