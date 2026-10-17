"""Observer-lite —— 卡住时的单次反思，跨切片/跨 attempt 持久化。

Heimdall 的外环观察者每 300s 读一次增量、产出 CONFIRMED / LOCK / ANGLES /
TENSION 注入下一轮 system。本模块把同一机制压进单进程 solver：

- 只在「连续多轮没有新事实」时触发（main.py 的 no-fact 分支），每 attempt
  最多 REFLECT_MAX 次，剩余时间不足时跳过；
- 只产出判断与方向，不产出命令/payload —— 执行权始终在 solver 主循环；
- 结果脱敏（flag 字面量）后写 out/observer.json，由编排器 collect_prior
  带入下一片/下一 attempt，经 common.setup_orch_mode 渲染成 # OBSERVER 块。

任何异常都返回 None，调用方回退到原有通用 nudge —— fail-open。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

REFLECT_MAX = int(os.environ.get("REFLECT_MAX") or "3")
REFLECT_MIN_REMAINING_S = float(os.environ.get("REFLECT_MIN_REMAINING_S") or "90")
REFLECT_TIMEOUT_S = float(os.environ.get("REFLECT_TIMEOUT_S") or "20")
MAX_HISTORY_MSGS = 14
MAX_HISTORY_CHARS = 3200
MAX_ITEM_CHARS = 200
MAX_LOCK_CHARS = 400

_FLAG_RE = re.compile(r"\b(?:flag|FLAG|HTB|CTF|PCTF|DUCTF|NSSCTF)\{[^{}\n]{1,200}\}")

_SYSTEM = (
    "You are the observer of a CTF solver. You do not execute commands and you do not "
    "write payloads. Read the recent execution evidence and judge the solve direction. "
    "Return ONLY a JSON object with keys: hypothesis (string), lock (string), "
    "confirmed (array of strings), angles (array of ordered strings), tension (array of "
    "at most 2 verbatim conflicting claims). Keep confirmed <= 6 and angles <= 5. "
    "Never include flag values, commands, or payloads. No markdown fences."
)


def redact(text: str) -> str:
    """去掉 flag 字面量（观察者输出不落任何真实 flag）。"""
    return _FLAG_RE.sub("[flag omitted]", str(text or ""))


def should_reflect(
    *,
    reflects: int,
    remaining_s: float | None,
    max_reflects: int | None = None,
    min_remaining_s: float | None = None,
) -> bool:
    limit = REFLECT_MAX if max_reflects is None else max_reflects
    floor = REFLECT_MIN_REMAINING_S if min_remaining_s is None else min_remaining_s
    if reflects >= limit:
        return False
    return not (remaining_s is not None and remaining_s < floor)


def _clip(text: object, limit: int) -> str:
    value = redact(str(text or "")).replace("\r", " ").strip()
    value = re.sub(r"\s+", " ", value)
    return value[:limit]


def _history(messages: list[dict]) -> str:
    lines: list[str] = []
    for message in messages[-MAX_HISTORY_MSGS:]:
        role = str(message.get("role") or "?")
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(str(item) for item in content)
        text = _clip(content, 600)
        calls = message.get("tool_calls") or []
        if calls:
            names = []
            for call in calls:
                fn = (call or {}).get("function") or {}
                args = str(fn.get("arguments") or "")[:180]
                names.append(f"{fn.get('name')}({args})")
            text = (text + " " if text else "") + "TOOLS: " + "; ".join(names)
        if text:
            lines.append(f"{role}: {text}")
    blob = "\n".join(lines)
    return blob[-MAX_HISTORY_CHARS:]


def build_prompt(
    task: dict,
    messages: list[dict],
    flags: list[str],
    remaining_s: float | None,
) -> list[dict]:
    task = task if isinstance(task, dict) else {}
    parts = [
        "Challenge description:",
        _clip(task.get("description"), 1200) or "(none)",
        f"Targets: {_clip(task.get('targets'), 300) or '(none)'}",
        f"Flags already submitted: {len(flags)}",
        f"Slice time left: {'unknown' if remaining_s is None else int(remaining_s)}s",
        "",
        "Recent execution evidence:",
        _history(messages) or "(none)",
        "",
        "Judge: what does this challenge test, where is it hard, where is the flag likely to "
        "live, which untried classes are still worth trying? Record what was actually tried.",
    ]
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


def parse(raw: object) -> dict | None:
    """把模型输出解析成规范化的 observer 记录；任何不合法返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    def items(key: str, limit: int = 5) -> list[str]:
        value = data.get(key)
        if not isinstance(value, list):
            return []
        out = []
        for item in value[:limit]:
            text_item = _clip(item, MAX_ITEM_CHARS)
            if text_item:
                out.append(text_item)
        return out

    obs = {
        "revision": int(time.time()),
        "ts": time.time(),
        "hypothesis": _clip(data.get("hypothesis"), MAX_ITEM_CHARS),
        "lock": _clip(data.get("lock"), MAX_LOCK_CHARS),
        "confirmed": items("confirmed", 6),
        "angles": items("angles", 5),
        "tension": items("tension", 2),
    }
    if not (obs["lock"] or obs["angles"] or obs["confirmed"]):
        return None
    return obs


def observe(
    client,
    model: str,
    task: dict,
    messages: list[dict],
    flags: list[str],
    remaining_s: float | None,
    *,
    timeout_s: float | None = None,
) -> dict | None:
    """一次反思调用。失败/超时/无 client 一律返回 None。"""
    if client is None:
        return None
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=build_prompt(task, messages, flags, remaining_s),
            max_tokens=420,
            temperature=0.2,
            timeout=REFLECT_TIMEOUT_S if timeout_s is None else timeout_s,
        )
        return parse(resp.choices[0].message.content)
    except Exception:  # noqa: BLE001 - 反思是增强，任何失败都回退
        return None


def save(out_dir: Path, obs: dict) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "observer.json"
        tmp = out_dir / "observer.json.tmp"
        tmp.write_text(json.dumps(obs, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def render(obs: dict) -> str:
    lines = ["# OBSERVER (advisory; new evidence beats this)"]
    if obs.get("hypothesis"):
        lines.append(f"HYPOTHESIS: {obs['hypothesis']}")
    if obs.get("confirmed"):
        lines.append("CONFIRMED:")
        lines.extend(f"- {item}" for item in obs["confirmed"])
    if obs.get("lock"):
        lines.append(f"LOCK: {obs['lock']}")
    if obs.get("angles"):
        lines.append("ANGLES:")
        lines.extend(f"- {item}" for item in obs["angles"])
    if obs.get("tension"):
        lines.append("TENSION:")
        lines.extend(f"- {item}" for item in obs["tension"])
    return "\n".join(lines) + "\n"
