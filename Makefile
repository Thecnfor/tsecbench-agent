# 开发任务入口。Windows 宿主机没有 make —— 在 Docker 开发容器里跑，
# 或直接执行各目标下的命令（uv 在 Windows 宿主机也可用）。
UV ?= uv

.PHONY: bootstrap test lint fmt check run mock image build-recorder build-mcp build-ts-workflow ui clean

## bootstrap: 安装 Python 3.13 并解析全部 workspace 依赖
bootstrap:
	$(UV) sync --all-packages

## test: 跑全部单测（当前：gateway 改写 + 令牌桶）
test:
	$(UV) run pytest -q

## lint: ruff 静态检查
lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

## fmt: ruff 自动格式化
fmt:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

## check: 快速接入自检（ing/ 摄取 + 平台 API 只读连通，零副作用）
check:
	$(UV) run python scripts/check_access.py

## run: 本地评测起编排器（先 make check；LLM key 取自 solver/.env，GATEWAY_MODE=off）
## 时间片可调：SLICE_MIN_S/SLICE_MAX_S 覆盖默认 120/1200（v1.1 起默认拉长到 300/1800）
SLICE_MIN_S ?= 300
SLICE_MAX_S ?= 1800
run:
	set -a; . ./solver/.env; set +a; SLICE_MIN_S=$(SLICE_MIN_S) SLICE_MAX_S=$(SLICE_MAX_S) $(UV) run python -m orchestrator

## rerun: 快速调试循环 —— 停掉在跑的编排器与 solver，后台重启（日志 /tmp/tsec-run.log）
## 注意：改 solver/ 下的代码本就实时生效（每题新起子进程），改完无需重启；
## 只有改 orchestrator/ 才需要这个（进程启动即把代码载入内存）。
## 重启走崩溃恢复：journal + 平台对账续跑，已解出的题不会丢。
rerun:
	-@pkill -f '/solver/main\.py$$' 2>/dev/null
	-@pkill -f 'python -m orchestrator$$' 2>/dev/null
	@sleep 1
	@set -a; . ./solver/.env; set +a; \
	 SLICE_MIN_S=$(SLICE_MIN_S) SLICE_MAX_S=$(SLICE_MAX_S) nohup $(UV) run python -m orchestrator >> /tmp/tsec-run.log 2>&1 & \
	 echo "orchestrator restarted -> tail -f /tmp/tsec-run.log  (journal: workspaces/journal.jsonl)"

## mock: 启动平台 mock server（runbook.md §4.1，只绑 loopback）
mock:
	$(UV) run python -m mock_server

## image: 构建完整交付镜像（需要 Docker Desktop；骨架阶段仅验证阶段依赖）
image:
	docker build -f docker/Dockerfile -t tsec-agent:latest .

## build-recorder: 只构建 Go 阶段（验证 recorder 可编译）
build-recorder:
	docker build -f docker/Dockerfile --target go-builder -t tsec-recorder:build .

## build-mcp: 只构建 Node 阶段（验证 TS→JS 编译链）
build-mcp:
	docker build -f docker/Dockerfile --target node-builder -t tsec-mcp:build .

## build-ts-workflow: 构建 L1.5 TS 手动工作流 solver（Mastra）
build-ts-workflow:
	cd solver/ts-workflow && npm install && npm run build

## ui: 启动调试观测台（http://127.0.0.1:7878，只读契约产物）
ui:
	UI_PORT=$${UI_PORT:-7878} node debug-ui/server.ts

clean:
	$(UV) run ruff clean 2>/dev/null; rm -rf .pytest_cache recorder/bin mcp/dist
