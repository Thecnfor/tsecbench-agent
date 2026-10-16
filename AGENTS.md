# AGENTS.md

TSecBench 自主渗透参赛 Agent：uv workspace（Python 3.14）+ Go recorder + Node MCP server，交付物是 Docker 镜像。设计文档 `docs/`（中文）是行为契约，入口 `docs/README.md`；代码注释大量按章节号引用它们（如 `architecture.md §6.5`），改行为前先读对应章节。**文档与代码冲突时以代码为准**（`README.md` 的「尚未开始实现」已过时：orchestrator/mock 已实现且全部单测绿）。

## 命令

- 首次（或加依赖后）必须先 `make bootstrap`（= `uv sync --all-packages`），否则 `uv run pytest` 直接 `ModuleNotFoundError: orchestrator/mock_server` —— workspace 成员包不会被默认 sync 装上
- `make test`：约 190 个单测约 40s，**完全离线**（e2e 在线程里起真 mock server + 假 solver；不需要 VPN、真 token、LLM key）
- 跑单个：`uv run pytest orchestrator/tests/test_gateway.py::TestWhitelist::test_exact_host_accepted`（testpaths 只含 `orchestrator/tests` + `mock/tests`，solver/mcp/recorder 没有测试）
- `make lint` 两关都要过：`ruff check` + `ruff format --check`；`make fmt` 一键修复两者。无 CI、无 pre-commit，提交前自查就是 `make lint && make test`
- `make check`：快速接入自检（`scripts/check_access.py`，只读探测平台 API，零副作用）
- `make run`：本地评测起编排器。**有真实副作用**（会 start 题目、消耗任务时限、真 LLM 调用）—— 冒烟验证用 `make check`，别随手 run；`make run` 会 source `solver/.env` 取 LLM key（key 只存在那里，gitignored）
- `make mock`：平台 mock，绑 `127.0.0.1:8787`，`MOCK_*` 环境变量控制限频/关卡门/故障注入（行为契约见 `runbook.md §4.1`）
- Go：`cd recorder && go build ./cmd/recorder`（零第三方依赖，无 go.sum）；Node：`cd mcp && npm ci && npm run build`（跳过 `npm ci` 会 `tsc: not found`）
- 镜像：`make image`（= `docker build -f docker/Dockerfile`）；`make build-recorder` / `make build-mcp` 只验证对应阶段。**`docker/Dockerfile` 是骨架**，runtime 阶段工具清单还是 TODO

## 结构与工具链

- workspace 三个成员（根 `pyproject.toml [tool.uv.workspace]`）：`orchestrator/` L0 编排器（src 布局，入口 `python -m orchestrator`）、`mock/` 平台 mock（`python -m mock_server`）、`solver/` L1 薄壳 —— **不是安装包**（`package = false`，脚本直接跑，自带 `.venv`/`.env`；ruff 对它豁免 E501，v0 原型不追排版）
- `toolchain.env` 是全部工具链版本的唯一真值源（Python 3.14 / Go 1.26 / Node 26.8.1 / REA 6.3.0 / Ghidra 12.1.4 …）—— 构建脚本与 Dockerfile 不得另写版本号，改版本 = 改这里 + 重跑受影响自检
- ruff：line-length 100、target py314、规则集 `E,F,I,UP,B,SIM`；格式化会产出 Python 3.14 语法（如无括号 `except A, B:`），不是 bug

## 接入（本地 = 官方下发 ing/，正式 = 环境变量 + 上传镜像）

- 每次测试平台下发一套新 `ing/`：`env` 文件是**冒号分隔，不是 dotenv** —— 值含 `https://`，只能在第一个冒号处切（`ingest.py parse_kv_line` 已按此实现；别用 `awk -F:`，见 `runbook.md §2.1` 的踩坑记录）+ `task_*_vpn_config.ovpn`（内含私钥）
- 本地流程：`make check` → `sudo openvpn --config ing/task_*_vpn_config.ovpn` → `make run`。托管模式**没有 `ing/`** 是正常状态，凭据全靠环境变量注入（Ingest Resolver 双路兼容）
- 托管必填环境变量及语义见 `runbook.md §3.3`；要点：`GATEWAY_MODE=on` 必开（本地默认 off 直连 DeepSeek）、`LLM_BASE_URL` 填未改写原始形态（改写全项目只在 `orchestrator/gateway.py` 一处）、`CONCURRENCY_LIMIT` 上限 3（启动即校验）
- 平台 API 细节以根目录 `CHALLENGES_API.md`（官方原文）为准；出站默认自我限速 ≤3 req/s（`API_MAX_QPS` 令牌桶）
- 防线断言（改 config/supervisor 时别绕过）：`BENCHMARK_TOKEN` 绝不进 solver 环境（`build_solver_env` 有 assert + 测试）；token 只留 L0，不落日志/报告

## 红线（平台按作弊处置）

- `ing/`（真实 token + VPN 私钥）与 `refs/`（467MB 第三方源码，3 个无 LICENSE、3 个 AGPL）已被 `.gitignore` 排除：**绝不 commit、绝不进镜像**；`.dockerignore` 第一条必须是 `refs/`（git 排除 ≠ 构建上下文排除，两个机制各自要配）
- 镜像与 prompt 不得含 flag 字面量、题目答案、针对性题解 —— 上届素材只学架构；构建末尾的逐层泄漏扫描（`image-spec.md §6`）必须保留
