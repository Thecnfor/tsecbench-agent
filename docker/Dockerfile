# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# TSecBench 交付镜像 · 多阶段骨架（image-spec.md §5）
#
#   go-builder   → CGO_ENABLED=0 编译 recorder；按 tag+sha256 下载 Go 静态工具
#   node-builder → npm ci（REA@钉死版本 + 自研 MCP server），TS 编译成纯 JS
#   runtime      → debian:trixie-slim，只有运行所需，不含任何语言工具链
#
# 版本一律取 toolchain.env（版本唯一真值源）。构建流程末尾有逐层泄漏扫描（§6）。
# ⚠️ 本文件当前是骨架：runtime 阶段的完整工具清单（image-spec.md §3）未填充。
# ─────────────────────────────────────────────────────────────────────────────

# ---- L2：Go 工具面 ----------------------------------------------------------
FROM golang:1.26 AS go-builder
WORKDIR /src
# recorder：唯一自研二进制（architecture.md §1.2）；零第三方依赖，无需 go.sum
COPY recorder/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/recorder ./cmd/recorder
# TODO(image build): 按 tag+sha256 下载 naabu / httpx / subfinder / nuclei / katana 静态二进制

# ---- L3：Node 能力面 --------------------------------------------------------
FROM node:26.11.1-trixie-slim AS node-builder
WORKDIR /src
# builder 阶段要装 devDeps（typescript）；产出只取 dist/，运行期不进 node_modules
# TODO(image build): REA@6.3.0 走 npm ci + lockfile；运行期禁 npx
COPY mcp/package.json mcp/package-lock.json ./
RUN npm ci
COPY mcp/ ./
RUN npm run build   # TS → 纯 JS（dist/），运行期不依赖原生 TS 直跑

# ---- L1 执行内核：pi vendored（运行期离线，禁 npx）---------------------------
# 版本取 toolchain.env（PI_PACKAGE / PI_VERSION，版本唯一真值源）；此处 ARG 默认
# 值仅为可读性，构建脚本必须以 --build-arg 传 toolchain.env 的值。
FROM node:26.11.1-trixie-slim AS pi-vendor
ARG PI_PACKAGE=@earendil-works/pi-coding-agent
ARG PI_VERSION=1.1.0
WORKDIR /opt/tsecbench/pi
RUN npm install --omit=dev "${PI_PACKAGE}@${PI_VERSION}" \
    && node ./node_modules/.bin/pi --version   # 装上即可执行，失败即断构建

# ---- L1.5：TS 手动工作流 solver（Mastra，版本钉 toolchain.env）--------------
FROM node:26.11.1-trixie-slim AS ts-workflow-builder
ARG MASTRA_VERSION=1.75.0
ARG ZOD_VERSION=4.6.5
WORKDIR /src
COPY solver/ts-workflow/package.json ./
# 依赖版本以 toolchain.env 为准（构建脚本 --build-arg 传入）；package.json 内
# 写死的版本仅为本地直跑兜底
RUN node -e "const p=require('./package.json');p.dependencies['@mastra/core']=process.env.MASTRA_VERSION||p.dependencies['@mastra/core'];p.dependencies['zod']=process.env.ZOD_VERSION||p.dependencies['zod'];require('fs').writeFileSync('package.json',JSON.stringify(p,null,2))" \
    && npm install --package-lock=false
COPY solver/ts-workflow/src/ ./src/
COPY solver/ts-workflow/tsconfig.json ./
RUN npm run build

# ---- runtime：运行时面 ------------------------------------------------------
FROM debian:trixie-slim AS runtime
# TODO(image build, 阻断项):
#   1. python-build-standalone 3.14.x（tag + sha256 钉死；trixie 自带 3.13 不可用）
#   2. apt 工具清单（image-spec.md §3：nmap / gdb / rizin / sqlmap / playwright+chromium …）
#   3. feroxbuster / ripgrep 静态二进制 + sha256
#   4. Ghidra 12.1.4 + JDK 21 + REA（本地路径，非 npx）
#   5. lua5.4（仅自检）、redis-tools、精选词库
#   6. 泄漏扫描（§6）必须作为构建的最后一道 RUN，命中即失败
COPY --from=go-builder /out/recorder /usr/local/bin/recorder
COPY --from=node-builder /src/dist/ /opt/tsecbench/mcp/
# L1 执行内核 + solver 源码（含 kb/ 知识库与 pi_ext/ 扩展；.env 不进镜像）
COPY --from=pi-vendor /opt/tsecbench/pi/ /opt/tsecbench/pi/
COPY solver/ /opt/tsecbench/solver/
# L1.5 TS 手动工作流（Mastra）：dist + node_modules + 元数据，源码不进 runtime
COPY --from=ts-workflow-builder /src/dist/ /opt/tsecbench/ts-workflow/dist/
COPY --from=ts-workflow-builder /src/node_modules/ /opt/tsecbench/ts-workflow/node_modules/
COPY --from=ts-workflow-builder /src/package.json /opt/tsecbench/ts-workflow/package.json
# 调试观测台（零依赖，只读 workspaces/ 契约产物）
COPY debug-ui/ /opt/tsecbench/debug-ui/
EXPOSE 7878
# solver 运行约束（supervisor 注入的环境变量，见 runbook.md §3.3）：
#   PI_BIN=/opt/tsecbench/pi/node_modules/.bin/pi  ← vendored pi，运行期禁 npx
#   SOLVER_CMD=node /opt/tsecbench/ts-workflow/dist/index.js   ← TS 工作流赛马入口
#   SOLVER_CMD=<python> /opt/tsecbench/solver/dual_main.py     ← pi 直跑赛马入口

# 入口 TODO: 编排器 main loop（architecture.md §3）
ENTRYPOINT ["/bin/bash", "-lc"]
