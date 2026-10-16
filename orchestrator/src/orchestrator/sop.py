"""EvoSOP 技能演化 —— 从成功轨迹蒸馏可复用 SOP（harness-v2.md §4）。

生命周期（§4.1）：build（蒸馏）→ merge（指纹去重合并）→ evaluate
（uses/wins 记账）→ prune（低胜率与超量出清）。存储在
`<workspace_root>/sops/`（运行期产物，不进镜像、不入仓库）：每条 SOP
一个 `.md` + `index.json` 记账。检索由 supervisor.prepare_workspace 在
新一轮建 workspace 时做（§4.3），经 task.json.sops 注入 APPEND_SYSTEM。

合规硬闸（§4.4，对应 architecture.md §10.4 / image-spec.md §6 红线）：
  - 平台禁止「针对题目的历史答题记忆」→ SOP 只允许**通用**步骤残片，
    构建期统一脱敏：IP/host/URL 打码、长 hex/base64 载荷截断；
  - 任何一行仍呈现 flag 形态（named patterns）→ 整条**拒绝写入**；
  - 零 LLM：提取 = 正则 + 集合运算，与 prior 合成同源。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .harvester import NAMED_FLAG_PATTERNS

SOP_DIR_NAME = "sops"
MAX_STEPS = 6  # 单条 SOP 最多保留的步骤数（§4.2 尺寸预算）
MAX_STEP_CHARS = 160
MAX_ENTRIES = 24  # 库容量：超量淘汰最旧低胜率条目
PRUNE_USES = 4  # 剪枝阈值：被用过这么多次仍几乎不中 → 出清
_PRUNE_KEEP_WINS = 1

_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?")
_HOST_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?){2,}\.[a-z]{2,}(?::\d{1,5})?")
_URL_RE = re.compile(r"\bhttps?://[^\s\"'<>]{4,200}")
_BLOB_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{60,}\b")  # 长 hex/base64 载荷
_CMD_RE = re.compile(r"^\$\s+(.{1,200})")
_WORD_RE = re.compile(r"[a-z0-9]{3,}")


def _sanitize(text: str) -> str:
    out = _URL_RE.sub("<URL>", text)
    out = _IP_RE.sub("<TARGET>", out)
    out = _HOST_RE.sub("<TARGET>", out)
    out = _BLOB_RE.sub("<PAYLOAD>", out)
    return out[:MAX_STEP_CHARS].strip()


def _steps(events: list[dict]) -> list[str]:
    """从 pi_stdout 事件流提取 `$ ` 命令行（与 prior 合成同源），脱敏后去重。"""
    steps: list[str] = []
    seen: set[str] = set()
    for ev in events:
        if ev.get("kind") != "pi_stdout":
            continue
        text = str(ev.get("text", ""))
        for line in text.splitlines():
            m = _CMD_RE.match(line.strip())
            if not m:
                continue
            step = _sanitize(m.group(1))
            if step and step not in seen:
                seen.add(step)
                steps.append(step)
            if len(steps) >= MAX_STEPS:
                return steps
    return steps


def _contains_flag(text: str) -> bool:
    return any(p.search(text) for p in NAMED_FLAG_PATTERNS)


def store_dir(base: Path) -> Path:
    return base / SOP_DIR_NAME


def _read_index(base: Path) -> dict:
    path = store_dir(base) / "index.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_index(base: Path, index: dict) -> None:
    d = store_dir(base)
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")


def _slugify(step: str, taken: set[str]) -> str:
    words = [w for w in _WORD_RE.findall(step.lower()) if w not in ("the", "for")]
    slug = "-".join(words[:3]) or "distilled-sop"
    base = slug
    n = 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug


def distill(ws: Path, base: Path, *, category: str = "general") -> dict | None:
    """从该题 workspace 的 session.jsonl 蒸馏 SOP 并并入库。

    任何合规硬闸命中（flag 形态存活 / 无步骤 / 脱敏后为空）→ 返回 None，
    库不动（fail-closed，§4.4）。返回入库条目 dict。
    """
    try:
        raw = (ws / "out" / "session.jsonl").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    events = []
    for line in raw.splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 半行事件安全跳过（与 gv/session.lines 同理）
    steps = _steps(events)
    if not steps:
        return None
    body = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    if _contains_flag(body):
        return None  # 硬闸：flag 字面量存活 → 整条拒绝

    index = _read_index(base)
    fingerprint = str(steps)[:200]
    for entry in index.values():
        if entry.get("fingerprint") == fingerprint:
            entry["wins"] = int(entry.get("wins", 0)) + 1
            entry["updated"] = round(time.time(), 1)
            _write_index(base, index)
            return entry

    d = store_dir(base)
    d.mkdir(parents=True, exist_ok=True)
    slug = _slugify(steps[0], set(index))
    (_d_md := d / f"{slug}.md").write_text(
        f"# SOP: {slug} (distilled, de-identified)\n\n{body}\n", encoding="utf-8"
    )
    entry = {
        "slug": slug,
        "file": f"{slug}.md",
        "fingerprint": fingerprint,
        "category": category,
        "uses": 0,
        "wins": 1,
        "updated": round(time.time(), 1),
    }
    index[slug] = entry
    _prune(base, index)
    _write_index(base, index)
    return entry


def _prune(base: Path, index: dict) -> None:
    """剪枝：uses>=PRUNE_USES 且 wins<=1 出清；库容量超限再踢低胜率旧条。"""
    for slug, entry in list(index.items()):
        spent = int(entry.get("uses", 0)) >= PRUNE_USES
        low = int(entry.get("wins", 0)) <= _PRUNE_KEEP_WINS
        if spent and low:
            index.pop(slug, None)
    if len(index) > MAX_ENTRIES:
        ordered = sorted(index.items(), key=lambda kv: (kv[1].get("wins", 0), kv[0]))
        for slug, _entry in ordered[: len(index) - MAX_ENTRIES]:
            index.pop(slug, None)
    # 附件一致性：正文文件丢失的条目直接出库（防孤儿索引）
    for slug, entry in list(index.items()):
        try:
            (store_dir(base) / entry["file"]).read_bytes()
        except (OSError, KeyError):
            index.pop(slug, None)


def mark_used(base: Path, slugs: list[str]) -> None:
    """评估记账：条目被注入消费即 uses+1（胜态由 distill 合并进 wins）。"""
    index = _read_index(base)
    for slug in slugs:
        entry = index.get(slug)
        if entry is not None:
            entry["uses"] = int(entry.get("uses", 0)) + 1
    _write_index(base, index)


def select(base: Path, text: str, *, top_k: int = 2) -> list[str]:
    """检索 top_k 条 SOP 正文（§4.3）：token 重叠 ×2 + wins 打分，确定性。"""
    index = _read_index(base)
    tokens = set(t for t in _WORD_RE.findall(text.lower()))
    scored: list[tuple[float, str]] = []
    for slug, entry in index.items():
        try:
            body = (store_dir(base) / entry["file"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _contains_flag(body):
            continue  # 检索侧同样过闸（历史脏数据防御）
        entry_tokens = set(t for t in _WORD_RE.findall(body.lower()))
        overlap = len(tokens & entry_tokens)
        score = overlap * 2 + int(entry.get("wins", 0))
        scored.append((score, slug))
    scored.sort(key=lambda s: (-s[0], s[1]))
    out: list[str] = []
    used: list[str] = []
    for _score, slug in scored[:top_k]:
        entry = index.get(slug) or {}
        path = store_dir(base) / str(entry.get("file", ""))
        try:
            out.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        used.append(slug)
    if used:
        mark_used(base, used)
    return out
