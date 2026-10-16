"""独立验证闸门（harness-v2-openhands.md §4）：模型不得自宣成功。

声称 ≠ 通过：workspace 里的 flag 形态内容只有同时通过
(a) harvester 同一正则复核 (b) shell 往返存活探测，才允许进 submit。
本模块零 LLM、零网络（runner 除外）。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .harvester import extract_flags  # 单一真值源：与收割同一组正则（NAMED + generic 兜底）


@dataclass
class VerifyReport:
    claimed: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    liveness: bool = False

    @property
    def passed(self) -> bool:
        return bool(self.confirmed) and self.liveness


def _shell_alive() -> bool:
    try:
        # dash 的 sh 无 $RANDOM（输出为空），用确定性回显验证 shell 往返
        p = subprocess.run(["sh", "-c", "echo ok"], capture_output=True, text=True, timeout=10)
        return p.returncode == 0 and p.stdout.strip() != ""
    except (OSError, subprocess.TimeoutExpired):
        return False


def verify_claim(ws: Path, *, runner: Callable[[], bool] | None = None) -> VerifyReport:
    alive = (runner or _shell_alive)()
    claimed: list[str] = []
    confirmed: list[str] = []
    flags_dir = ws / "out" / "flags"
    if flags_dir.is_dir():
        for f in sorted(flags_dir.iterdir()):
            if not f.is_file():
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            for token in extract_flags(text):
                if token not in claimed:
                    claimed.append(token)
                    if alive:
                        confirmed.append(token)
    return VerifyReport(claimed=claimed, confirmed=confirmed, liveness=alive)
