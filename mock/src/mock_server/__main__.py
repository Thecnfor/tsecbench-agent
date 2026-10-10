"""mock server 入口：`uv run python -m mock_server` 或 `python mock/src/mock_server/__main__.py`。

环境变量见 state.build_state 与 runbook.md §4.1：
  MOCK_TOKEN / MOCK_PORT / MOCK_START_DELAY_S（题面已内建）/ MOCK_CHALLENGES_JSON
  MOCK_RATE_LIMIT / MOCK_LEVEL_GATE / MOCK_TOTAL_LIMIT_S
"""

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
