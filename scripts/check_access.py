#!/usr/bin/env python3
"""快速接入自检：ing/ 摄取 + 平台 API 只读连通性（不 start、不 submit，零副作用）。

    uv run python scripts/check_access.py

验证三件事（对应 runbook.md §2 的本地接入链路）：
  1. Ingest Resolver 能从 ing/env + ing/*.ovpn 解析出 token/base_url/vpn（形状归一化）；
  2. 平台 API 只读连通：GET /openapi/v1/challenges 返回题目列表；
  3. 汇总题目状态（container_status 分布），供判断是否需要先拨靶场 VPN。

凭据只经 L0 内存，不打印、不落盘；本脚本绝不调用 start/submit/hint/close。
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "orchestrator" / "src"))

from orchestrator.api import PlatformClient  # noqa: E402
from orchestrator.ingest import resolve_ingest  # noqa: E402


def _ingest() -> tuple[str, str, list[str]]:
    result = resolve_ingest()
    print(f"[1/2] ing/ 摄取 OK  token=***{result.token[-4:]} (len={len(result.token)})")
    print(f"      base_url={result.base_url}")
    print(f"      vpn_configs={[Path(p).name for p in result.vpn_configs] or '无'}")
    return result.token, result.base_url, result.vpn_configs


async def _probe(token: str, base_url: str) -> None:
    client = PlatformClient(base_url=base_url, token=token)
    challenges = await client.list_challenges()
    statuses = Counter(c.get("container_status", "?") for c in challenges)
    pending = [c for c in challenges if not c.get("is_completed")]
    print(f"[2/2] 平台 API 连通 OK  题目总数={len(challenges)}  未完成={len(pending)}")
    print(f"      container_status 分布: {dict(statuses)}")
    sample = ", ".join(c["unique_code"] for c in pending[:5])
    print(f"      未完成前 5 题: {sample or '无'}")
    print("\n接入自检通过。本地快速测试下一步（runbook.md §2）:")
    print("  1) 拨靶场 VPN（需要 sudo，容器地址仅 VPN 内可达）:")
    print("     sudo openvpn --config ing/task_*_vpn_config.ovpn")
    print("  2) 起编排器本地模式（GATEWAY_MODE=off，直连 DeepSeek）:")
    print("     make run")


def main() -> int:
    try:
        token, base_url, _ = _ingest()
    except (ValueError, FileNotFoundError) as exc:
        print(f"[FAIL] ing/ 摄取失败: {exc}", file=sys.stderr)
        return 1
    try:
        asyncio.run(_probe(token, base_url))
    except Exception as exc:  # noqa: BLE001 - 自检脚本：任何网络错误都要可读地报出
        # ApiError 的响应体摘要已内联在异常消息里（api.py 错误体系），直接打印
        print(f"[FAIL] 平台 API 探测失败: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
