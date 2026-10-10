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
FROM node:26.8.1-slim AS node-builder
WORKDIR /src
# builder 阶段要装 devDeps（typescript）；产出只取 dist/，运行期不进 node_modules
# TODO(image build): REA@6.3.0 走 npm ci + lockfile；运行期禁 npx
COPY mcp/package.json mcp/package-lock.json ./
RUN npm ci
COPY mcp/ ./
RUN npm run build   # TS → 纯 JS（dist/），运行期不依赖原生 TS 直跑

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

# 入口 TODO: 编排器 main loop（architecture.md §3）
ENTRYPOINT ["/bin/bash", "-lc"]
