# 开发任务入口。Windows 宿主机没有 make —— 在 Docker 开发容器里跑，
# 或直接执行各目标下的命令（uv 在 Windows 宿主机也可用）。
UV ?= uv

.PHONY: bootstrap test lint fmt check run mock image build-recorder build-mcp clean

## bootstrap: 安装 Python 3.14 并解析全部 workspace 依赖
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

clean:
	$(UV) run ruff clean 2>/dev/null; rm -rf .pytest_cache recorder/bin mcp/dist
