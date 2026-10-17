"""Prior 合成 —— 从 session 事件流 + 候选确定性生成 out/prior.json。

编排器 Harvester 对 prior.json 容忍缺失/畸形（§2.3 不信任 solver 自述），
本模块的价值是把「本轮做过什么」结构化回灌给下一段 attempt：
  assets      —— 事件里出现过的 host:port / URL（客观资产）
  explored    —— 已执行的命令首词序列（防下轮重复侦察）
  open_leads  —— 未验证的线索（输出中出现但未产出 flag 的 URL/版本号）
  credentials —— 输出中的凭据/令牌
  findings    —— 输出中的版本/CVE 线索
提取逻辑收敛在 solver/state.py，双内核共用（零 LLM）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def synthesize(events: list[dict], candidates: list[str]) -> dict:
    solver_dir = str(Path(__file__).resolve().parents[1])
    if solver_dir not in sys.path:
        sys.path.insert(0, solver_dir)
    from state import synthesize_prior

    return synthesize_prior(events, candidates)


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
