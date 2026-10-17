# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# TSecBench 决赛 Harness 交付镜像（image-spec.md §5 / harness-v2.md）
#
#   go-builder            → CGO_ENABLED=0 编译 recorder
#   node-builder          → npm ci（自研 MCP server），TS → 纯 JS
#   pi-vendor             → vendored pi 执行内核（运行期离线，禁 npx）
#   ts-workflow-builder   → Mastra 手动工作流（TS → dist）
#   runtime               → debian:trixie-slim + Python 3.13 + Node + 工具链
#                           + OpenHands 控制平面（独立 venv）
#
# 版本一律取 toolchain.env（版本唯一真值源）；构建脚本必须以 --build-arg 传入，
# 本文件里的 ARG 默认值仅为可读性兜底。末尾有逐层泄漏扫描（§6），命中即断构建。
#
# 架构注记：本机（aarch64）只做语法/依赖验证，不构建镜像；正式构建在 amd64。
# ─────────────────────────────────────────────────────────────────────────────

ARG NODE_VERSION=26.11.1
ARG GO_VERSION=1.26
ARG PYTHON_VERSION=3.13
ARG PI_PACKAGE=@earendil-works/pi-coding-agent
ARG PI_VERSION=1.1.0
ARG OPENHANDS_VERSION=1.11.0
ARG MASTRA_VERSION=1.75.0
ARG ZOD_VERSION=4.6.5
ARG RIPGREP_VERSION=TODO-PIN-AT-IMAGE-BUILD
ARG FEROXBUSTER_VERSION=TODO-PIN-AT-IMAGE-BUILD

# ---- L2：Go 工具面 ----------------------------------------------------------
FROM golang:${GO_VERSION} AS go-builder
WORKDIR /src
# recorder：唯一自研二进制（architecture.md §1.2）；零第三方依赖，无需 go.sum
COPY recorder/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/recorder ./cmd/recorder

# ---- L3：Node 能力面 --------------------------------------------------------
FROM node:${NODE_VERSION}-trixie-slim AS node-builder
WORKDIR /src
COPY mcp/package.json mcp/package-lock.json ./
RUN npm ci
COPY mcp/ ./
RUN npm run build   # TS → 纯 JS（dist/），运行期不依赖原生 TS 直跑

# ---- L1 执行内核：pi vendored（运行期离线，禁 npx）---------------------------
FROM node:${NODE_VERSION}-trixie-slim AS pi-vendor
ARG PI_PACKAGE
ARG PI_VERSION
WORKDIR /opt/tsecbench/pi
RUN npm install --omit=dev "${PI_PACKAGE}@${PI_VERSION}" \
    && ./node_modules/.bin/pi --version   # 装上即可执行，失败即断构建
# 悬空 tool_calls 消毒补丁（deepseek 400 死循环；见 solver/THIRD_PARTY.md）
COPY docker/pi_patch.js /tmp/pi_patch.js
RUN node /tmp/pi_patch.js /opt/tsecbench/pi/node_modules/@earendil-works/pi-coding-agent \
    && rm /tmp/pi_patch.js

# ---- L1.5：TS 手动工作流 solver（Mastra）------------------------------------
FROM node:${NODE_VERSION}-trixie-slim AS ts-workflow-builder
ARG MASTRA_VERSION
ARG ZOD_VERSION
WORKDIR /src
COPY solver/ts-workflow/package.json ./
# 依赖版本以 toolchain.env 为准；package.json 内写死的版本仅为本地直跑兜底
RUN node -e "const p=require('./package.json');p.dependencies['@mastra/core']=process.env.MASTRA_VERSION||p.dependencies['@mastra/core'];p.dependencies['zod']=process.env.ZOD_VERSION||p.dependencies['zod'];require('fs').writeFileSync('package.json',JSON.stringify(p,null,2))" \
    && npm install --package-lock=false
COPY solver/ts-workflow/src/ ./src/
COPY solver/ts-workflow/tsconfig.json ./
RUN npm run build

# ---- runtime：运行时面 ------------------------------------------------------
FROM debian:trixie-slim AS runtime
ARG PYTHON_VERSION
ARG OPENHANDS_VERSION
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/usr/local/bin:/opt/tsecbench/venv/bin:$PATH

# 1) 系统工具面（image-spec.md §3）。python3.13 是 trixie 自带，无需 standalone 包。
RUN apt-get update && apt-get install -y --no-install-recommends \
      # 基础 / 网络
      ca-certificates ripgrep curl wget git unzip xz-utils file xxd less procps \
      socat netcat-openbsd tcpdump iproute2 dnsutils \
      # 侦察与扫描
      nmap masscan \
      # Web
      sqlmap whatweb \
      # 爆破
      hydra john hashcat \
      # 多阶段 / 横向 / 云 / 取证
      openssh-client sshpass proxychains4 jq smbclient ldap-utils \
      libimage-exiftool-perl steghide \
      # 二进制 / Pwn
      gdb binutils patchelf gcc g++ make gcc-multilib \
      # 逆向（trixie 无 rizin/radare2，用 gdb+binutils+binwalk；nikto 也不在 trixie，
      #   image-spec.md §3 记为 apt/git，需要时单独 clone）
      binwalk \
      # L4 Lua 注入面（仅本地写/自检 .nse）+ Redis
      lua5.4 redis-tools \
      # Python 3.13 运行时（trixie 自带）
      python3 python3-venv python3-pip \
      # JDK（Ghidra 12.1.4 依赖）
      openjdk-21-jdk-headless \
      # VPN 双模（runbook.md §2.2）
      openvpn \
    && rm -rf /var/lib/apt/lists/* \
    && python3 --version \
    # nmap NSE 脚本必须完整保留（image-spec.md §3：>500 个 .nse）
    && test "$(ls /usr/share/nmap/scripts/*.nse | wc -l)" -gt 500

# 1b) 静态 Web 工具（apt 没有的 ffuf/nuclei/chisel）
RUN set -eux; \
    curl -fsSL https://github.com/ffuf/ffuf/releases/download/v2.1.0/ffuf_2.1.0_linux_amd64.tar.gz \
      | tar -xz -C /tmp ffuf && install -m 0755 /tmp/ffuf /usr/local/bin/ffuf; \
    curl -fsSL https://github.com/projectdiscovery/nuclei/releases/download/v3.3.5/nuclei_3.3.5_linux_amd64.zip \
      -o /tmp/nuclei.zip && unzip -q /tmp/nuclei.zip -d /tmp && install -m 0755 /tmp/nuclei /usr/local/bin/nuclei; \
    curl -fsSL https://github.com/jpillora/chisel/releases/download/v1.10.1/chisel_1.10.1_linux_amd64.gz \
      | gunzip > /usr/local/bin/chisel && chmod 0755 /usr/local/bin/chisel; \
    rm -rf /tmp/ffuf /tmp/nuclei.zip /tmp/nuclei

# 2) Node 运行时（pi / ts-workflow 需要）—— 从 pi-vendor 阶段带过来
COPY --from=pi-vendor /usr/local/bin/node /usr/local/bin/node
COPY --from=pi-vendor /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version

# 3) Python 依赖：编排器（本项目）+ OpenHands 控制平面（独立 venv，版本取
#    toolchain.env）。OpenHands 全系 requires_python <3.14 —— 这是本镜像钉 3.13
#    的唯一原因（architecture.md §1.1）。
# 只带 pyproject + src：tests/ 里的样例 flag 不应进任何镜像层
COPY orchestrator/pyproject.toml /tmp/build/orchestrator/pyproject.toml
COPY orchestrator/src/ /tmp/build/orchestrator/src/
COPY mock/ /tmp/build/mock/
COPY pyproject.toml uv.lock /tmp/build/
RUN python3 -m venv /opt/tsecbench/venv \
    && /opt/tsecbench/venv/bin/pip install --upgrade pip \
    # triviumdb 发的是 abi3 轮子、3.13 可用，但元数据保守写了 <3.13
    && /opt/tsecbench/venv/bin/pip install --ignore-requires-python "triviumdb>=0.8.8,<0.9" \
    && /opt/tsecbench/venv/bin/pip install /tmp/build/orchestrator /tmp/build/mock \
    && /opt/tsecbench/venv/bin/pip install "openhands-ai==${OPENHANDS_VERSION}" \
    && /opt/tsecbench/venv/bin/pip install pwntools capstone pyelftools ROPgadget impacket awscli \
    && /opt/tsecbench/venv/bin/python -c "import orchestrator, mock_server, openhands, pwn, capstone, elftools; print('py deps ok')" \
    && rm -rf /tmp/build

# 4) 交付物落位
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

# solver 运行约束（supervisor 注入，见 runbook.md §3.3）：
#   PI_BIN=/opt/tsecbench/pi/node_modules/.bin/pi  ← vendored pi，运行期禁 npx
#   SOLVER_CMD=node /opt/tsecbench/ts-workflow/dist/index.js   ← TS 工作流入口
#   SOLVER_CMD=<python> /opt/tsecbench/solver/dual_main.py     ← pi 直跑入口
ENV PI_BIN=/opt/tsecbench/pi/node_modules/.bin/pi \
    GATEWAY_MODE=on \
    TSECBENCH_SOLVER_DIR=/opt/tsecbench/solver

# 5) 泄漏扫描（image-spec.md §6）—— 必须是最后一道 RUN，命中即断构建。
#    红线：镜像不得含 flag 字面量、题目答案、针对性题解、ing/ 凭据、refs/ 素材。
RUN set -eu; \
    fail=0; \
    # ing/ 与 refs/ 绝不进镜像（.dockerignore 已挡，这里做纵深防御）
    for p in /ing /refs /opt/tsecbench/ing /opt/tsecbench/refs; do \
      [ -e "$p" ] && { echo "LEAK: $p present"; fail=1; }; \
    done; \
    # 凭据文件不得进镜像
    find / -xdev \( -name '.env' -o -name '*.ovpn' -o -name 'task_*_vpn_config*' \) \
      -not -path '*/node_modules/*' -not -path '/opt/tsecbench/solver/.env' 2>/dev/null \
      | grep . && { echo "LEAK: credential-ish files found"; fail=1; }; \
    # flag 字面量（题目答案）不得进镜像
    grep -rIlE 'flag\{[0-9a-f]{8}-[0-9a-f]{4}' /opt/tsecbench /usr/local/bin 2>/dev/null \
      && { echo "LEAK: flag literal found"; fail=1; }; \
    [ "$fail" -eq 0 ] || exit 1; \
    echo "leak scan: clean"

# 入口：编排器 main loop（architecture.md §3）。托管模式下平台注入环境变量，
# 无 ing/ 是正常状态（runbook.md §3.3）。
ENTRYPOINT ["/opt/tsecbench/venv/bin/python", "-m", "orchestrator"]
