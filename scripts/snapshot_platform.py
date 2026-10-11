#!/usr/bin/env python3
"""平台状态时间线快照器（本地评测专用，零副作用只读）。

每 POLL_SECONDS 秒 GET /openapi/v1/challenges，把完整行 + 聚合指标追加到
workspaces/platform_timeline.jsonl —— 供赛后与 journal 事件对齐，分析 harness
调度/收割/放弃决策。凭据从 ing/ 解析（首个冒号切分，绝不 eval）。

用法: setsid nohup python3 scripts/snapshot_platform.py >/dev/null 2>&1 &
"""

import json
import os
import time
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "workspaces" / "platform_timeline.jsonl"
POLL_SECONDS = 30


def read_ingest() -> tuple[str, str]:
    kv: dict[str, str] = {}
    env_file = ROOT / "ing" / "env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                kv[k.strip()] = v.strip()
    token = kv.get("BENCHMARK_TOKEN") or os.environ.get("BENCHMARK_TOKEN", "")
    base = kv.get("BENCHMARK_BASE_URL") or os.environ.get(
        "BENCHMARK_BASE_URL", "https://tsecbench.zc.tencent.com"
    )
    return token, base.rstrip("/")


def snapshot(token: str, base: str) -> dict:
    req = urllib.request.Request(
        f"{base}/openapi/v1/challenges", headers={"BENCHMARK_TOKEN": token}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        rows = json.load(resp)
    status = Counter(r.get("container_status") for r in rows)
    return {
        "ts": time.time(),
        "aggregate": {
            "completed": sum(1 for r in rows if r.get("is_completed")),
            "flags_total": sum(r.get("correct_flag_count", 0) for r in rows),
            "container_status": dict(status),
        },
        "challenges": rows,
    }


def main() -> None:
    token, base = read_ingest()
    if not token:
        raise SystemExit("no BENCHMARK_TOKEN in ing/env or environment")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    errors = 0
    while True:
        try:
            record = snapshot(token, base)
            errors = 0
        except Exception as exc:  # 网络抖动/限频：记错继续，不死
            errors += 1
            record = {"ts": time.time(), "error": f"{type(exc).__name__}: {exc}", "streak": errors}
        with OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
