"""启动期规划（planner）—— 全程唯一一次额外 LLM 调用。

输入：task.json 摘要 + prior + 知识索引（Phase 4 接入）→ 输出：题型确认 +
3~5 条策略要点，注入 APPEND_SYSTEM.md。硬约束（plan 风险表）：
  - 无 API key / 剩余时间 <90s / LLM 失败 → 静默跳过，绝不阻塞解题；
  - 输出截断，防止规划调用本身吃掉时间片。
"""

from __future__ import annotations

import json
import os
import time

MAX_STRATEGY_CHARS = 2000
MIN_REMAINING_S = 90.0

_PROMPT = """You are the planner of a pentest CTF solver. Given the challenge summary,
pick the challenge category (one of: web, pwn, reverse, forensics, misc) and give
3-5 concrete first-step strategy bullets. Be terse. Reply ONLY with JSON:
{{"category": "...", "strategy": ["...", "..."]}}

Challenge summary:
{summary}
"""


def build_summary(task: dict) -> str:
    parts = []
    if task.get("description"):
        parts.append("description: " + str(task["description"])[:1500])
    if task.get("targets"):
        parts.append("targets: " + json.dumps(task["targets"], ensure_ascii=False))
    prior = task.get("prior") or {}
    for key in ("assets", "explored", "open_leads"):
        items = prior.get(key) or []
        if items:
            parts.append(f"{key}: " + json.dumps(items, ensure_ascii=False)[:1000])
    return "\n".join(parts) or "(empty task)"


def plan(task: dict, deadline_epoch: float | None) -> dict | None:
    """返回 {"category","strategy"} 或 None（跳过）。永不抛异常。"""
    if not (os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")):
        return None
    # deadline 检查自身也不得抛（本函数契约：任何路径失败 → 静默跳过）。
    # SOLVER_DEADLINE_EPOCH 是字符串环境变量，先显式转 float。
    try:
        if deadline_epoch is not None:
            deadline_epoch = float(deadline_epoch)  # noqa: F811 - 归一化入参类型
            if deadline_epoch - time.time() < MIN_REMAINING_S:
                return None
    except (TypeError, ValueError):
        deadline_epoch = None
    try:
        from openai import OpenAI  # 与 main.py 同款惰性导入

        client = OpenAI(
            api_key=os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"),
            base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            timeout=30.0,
            max_retries=1,
        )
        resp = client.chat.completions.create(
            model=os.environ.get("LLM_MODEL", "deepseek-chat"),
            messages=[
                {"role": "system", "content": "Reply with JSON only."},
                {"role": "user", "content": _PROMPT.format(summary=build_summary(task))},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        # 容忍 ```json 围栏
        if raw.startswith("```"):
            raw = raw.strip("`").removeprefix("json").strip()
        data = json.loads(raw)
        strategy = " ".join(str(x) for x in (data.get("strategy") or []))[:MAX_STRATEGY_CHARS]
        category = str(data.get("category") or "").strip().lower()
        return {"category": category, "strategy": strategy}
    except Exception:
        return None
