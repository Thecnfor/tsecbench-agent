"""Flag Harvester + Submitter（architecture.md §3 / §2.3）。

收割纪律（§2.3 强约束）：
  - **编排器不信任 result.json** —— 直接正则扫 workspace 全部文件产物 +
    stdout/stderr 落盘文件 + out/traffic/*.jsonl（recorder 通道，将来接入）；
  - 候选长度 1~4096（platform-api.md §4.9），先过滤再提交，避免 422；
  - 去重后按发现顺序提交。

提交纪律（§5.1）：
  - duplicate **不算失败**：标记该候选已入袋，继续下一个；
  - 进度真值是 submit 响应的 correct_flag_count / matched_flag_index；
  - 422 FlagRejected → 丢弃该候选继续（理论上已被长度过滤挡住）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .api import Duplicate, FlagRejected, PlatformClient, TaskEnded
from .registry import ChallengeRecord

# 与 solver/main.py 保持同一组命名模式 + 通用兜底
NAMED_FLAG_PATTERNS = (
    re.compile(r"(?<![a-zA-Z])FLAG\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])flag\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])HTB\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])picoCTF\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])THM\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])CTF\{[^}\n]{1,200}\}"),
    re.compile(r"(?<![a-zA-Z])XBEN\{[^}\n]{1,200}\}"),
)
# 通用兜底必须拒绝 CSS/JS 规则形（body{...}、h1{border:...}、let{...}）：
# 线上 run-35236 实测 stdout 里的 CSS 被当作 flag 连投 10 次全部失败。
GENERIC_FLAG_PATTERN = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9_]{2,24})\{([^}\n]{4,120})\}")
GENERIC_BODY_RE = re.compile(r"^[A-Za-z0-9_\-+=/.]{4,120}$")
GENERIC_PREFIXES = frozenset({"flag", "ctf", "htb", "thm", "picoctf", "xben"})
_PLACEHOLDER_BODIES = frozenset(
    {"...", "<...>", "flag", "example", "your_flag_here", "your_flag", "xxx", "redacted"}
)


def _is_plausible(flag: str) -> bool:
    if "{" not in flag or "}" not in flag:
        return False
    body = flag.split("{", 1)[1].rsplit("}", 1)[0].strip()
    if not body or body.lower() in _PLACEHOLDER_BODIES:
        return False
    if "<" in body or ">" in body:
        return False
    if any(ch in body for ch in "[]^()|\\*+?"):
        return False
    return not set(body) <= set(".*-_ ")


FLAG_MIN_LEN, FLAG_MAX_LEN = 1, 4096
_SCAN_SUFFIX_BLOCKLIST = (".log.bak",)  # 无实际用途，占位说明只按内容扫
_MAX_FILE_BYTES = 64 * 1024 * 1024  # 单文件扫描上限，防海量录制撑爆内存


def extract_flags(text: str) -> list[str]:
    """从一段文本提取候选：命名模式优先，generic 兜底；保持发现顺序，去重。"""
    found: list[str] = []
    seen: set[str] = set()
    for pattern in NAMED_FLAG_PATTERNS:
        for m in pattern.finditer(text):
            value = m.group(0)
            if not _is_plausible(value):
                continue
            if value not in seen:
                seen.add(value)
                found.append(value)
    for m in GENERIC_FLAG_PATTERN.finditer(text):
        if m.group(1).lower() not in GENERIC_PREFIXES:
            continue
        if not GENERIC_BODY_RE.match(m.group(2)):
            continue
        value = m.group(0)
        if value not in seen:
            seen.add(value)
            found.append(value)
    return found


def _read_head(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return path.read_text(encoding="utf-8", errors="replace")[:_MAX_FILE_BYTES]
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def harvest(ws: Path) -> list[str]:
    """双通道收割：workspace 全部文本文件（含 out/flags、notes、stdout/stderr、
    out/traffic/*.jsonl）。result.json 不排除 —— 它只是不可信，扫了也无妨。

    返回去重后的候选（发现顺序：文件名字典序 × 文件内出现序）。
    """
    ws / "out"
    candidates: list[str] = []
    seen: set[str] = set()
    files: list[Path] = []
    if ws.is_dir():
        files = [p for p in ws.rglob("*") if p.is_file() and p.suffix not in _SCAN_SUFFIX_BLOCKLIST]
    for path in sorted(files):
        text = _read_head(path)
        for flag in extract_flags(text):
            if flag not in seen:
                seen.add(flag)
                candidates.append(flag)
    return [f for f in candidates if FLAG_MIN_LEN <= len(f) <= FLAG_MAX_LEN]


@dataclass(slots=True)
class SubmitReport:
    """提交轮次的记账结果。"""

    new_matches: int = 0
    duplicates: int = 0
    rejected: int = 0
    completed: bool = False
    task_ended: bool = False
    matched_indices: list[int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.matched_indices is None:
            self.matched_indices = []


async def submit_candidates(
    client: PlatformClient, rec: ChallengeRecord, candidates: list[str]
) -> SubmitReport:
    """逐个提交候选。duplicate 不算失败；任务级终止立即停（§5.1）。"""
    report = SubmitReport()
    for flag in candidates:
        try:
            result = await client.submit(rec.unique_code, flag)
        except Duplicate:
            report.duplicates += 1
            rec.submitted.add(flag)  # 已入袋，避免每个收割节拍重投
            continue
        except FlagRejected:
            report.rejected += 1
            rec.rejected_candidates += 1
            rec.submitted.add(flag)  # 失败也是终态；否则 45s 后会重复投递
            continue
        except TaskEnded:
            report.task_ended = True
            break
        idx = result.get("matched_flag_index")
        if idx is not None and int(idx) not in rec.matched_indices:
            rec.matched_indices.add(int(idx))
            report.matched_indices.append(int(idx))
            report.new_matches += 1
        rec.submitted.add(flag)
        # 多 flag 题：correct_flag_count 是进度真值（platform-api.md §4.6）
        total = result.get("total_flag_count")
        if total and result["correct_flag_count"] >= total:
            report.completed = True
            break
    return report
