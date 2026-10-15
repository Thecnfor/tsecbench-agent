# 镜像大小硬约束：push 压缩后 ≤ 3GB。**别用 `docker images` 的数字判超标**——它显示的是
# 原始（未压缩）体积，与 3GB 口径不同（实测比值约 4~5x）。压缩实测：
#   docker save tsec-agent:latest | gzip -1 -c | wc -c
# 选包原则：按领域覆盖、同类去重（每类留代表工具）；题目特化资源一律不进镜像。
#
# 两阶段构建：阶段1 全量安装+裁剪（裁掉的文件留在中间层）；阶段2 只 COPY 最终
# 文件系统——否则 seclists 这种"装 1.9G 再删"会把镜像层撑爆。
# 已知连带依赖教训（勿再"无意间"引入）：
#   commix → unicorn-magic → metasploit(632M) + oracle-instantclient(206M) + mingw(490M)
#   hashcat → pocl-opencl-icd(127M)（容器无 GPU，hashcat 无意义，用 john 代替）
#   注：metasploit 现在是**有意安装**（见文件尾的独立层），但别再靠 commix 这类间接带进来，
#   也不要为了它去装 postgresql-server（msf 无库可用，DB 功能受限即可）。

FROM kalilinux/kali-rolling AS builder

ENV DEBIAN_FRONTEND=noninteractive

# 固定国内 Kali 镜像：http.kali.org 会 302 到 mirrors.neusoft.edu.cn（仅 IPv6，
# 容器内解析失败）；TUNA/USTC/阿里云可达。用 http 而非 https——apt update 阶段
# ca-certificates 尚未安装，https 会证书校验失败
RUN set -eux; \
    printf 'Types: deb\nURIs: http://mirrors.tuna.tsinghua.edu.cn/kali\nSuites: kali-rolling\nComponents: main contrib non-free non-free-firmware\nSigned-By: /usr/share/keyrings/kali-archive-keyring.gpg\n' \
        > /etc/apt/sources.list.d/kali.sources; \
    printf 'deb http://mirrors.ustc.edu.cn/kali kali-rolling main contrib non-free non-free-firmware\ndeb http://mirrors.aliyun.com/kali kali-rolling main contrib non-free non-free-firmware\n' \
        > /etc/apt/sources.list.d/kali-backup.list; \
    rm -f /etc/apt/sources.list

# ── 按领域安装（同类去重；见文件头"连带依赖教训"）────────────────────
# 领域：Web / 侦察 / 内网协议 / 爆破 / 逆向 / 流量 / 取证隐写 / 数据库 /
#       隧道 / 漏洞库 / 杂项 / 基础运行时
RUN apt-get update && apt-get install -y --no-install-recommends \
        nmap masscan whatweb \
        sqlmap gobuster ffuf dirb nikto \
        wpscan wafw00f sslscan \
        hydra john sshpass \
        openvpn \
        curl wget ca-certificates \
        netcat-openbsd dnsutils xxd jq git \
        python3 python3-pip python3-venv \
        python3-requests python3-pil python3-scapy python3-cryptography \
        procps iproute2 iputils-ping traceroute tcpdump \
        smbclient enum4linux nbtscan ldap-utils snmp python3-impacket \
        proxychains4 socat chisel \
        redis-tools sqlite3 default-mysql-client postgresql-client \
        tshark arp-scan hping3 net-tools \
        binutils file gdb strace ltrace radare2 ropper python3-pwntools \
        binwalk libimage-exiftool-perl steghide foremost \
        tesseract-ocr tesseract-ocr-eng \
        exploitdb \
        wordlists seclists \
        expect rlwrap lsof p7zip-full zip unzip rsync tmux vim-tiny \
    && rm -rf /var/lib/apt/lists/* \
    && (gzip -d /usr/share/wordlists/rockyou.txt.gz 2>/dev/null || true)

# ── 字典裁剪（在 builder 层做，删除量不进最终镜像）─────────────────────
RUN find /usr/share/seclists -mindepth 1 -maxdepth 1 \
        ! -name "Discovery" ! -name "Payloads" ! -name "Passwords" -exec rm -rf {} + ; \
    find /usr/share/seclists/Discovery -mindepth 1 -maxdepth 1 \
        ! -name "Web-Content" -exec rm -rf {} + ; \
    find /usr/share/seclists -type f -size +2M -delete ; \
    find /usr/share/exploitdb -maxdepth 1 -type d ! -name exploitdb \
        ! -name "exploits" ! -name "shellcodes" ! -name "www" -exec rm -rf {} + 2>/dev/null || true ; \
    rm -rf /usr/share/doc /usr/share/man /usr/share/locale /usr/share/lintian \
           /usr/share/bug /usr/share/omf /var/log/* 2>/dev/null ; \
    true

# ── Python 依赖（与模型运行时 pip install 的包对齐，限网 already-satisfied）──
RUN pip3 install --break-system-packages --no-cache-dir \
        requests flask itsdangerous flask-unsign pydash cryptography pyjwt scapy \
    || pip3 install --no-cache-dir requests flask itsdangerous flask-unsign pydash cryptography pyjwt scapy

# ── 出站白名单防火墙（限网模式 entrypoint 下发规则用；独立层保前面缓存）──
RUN apt-get update && apt-get install -y --no-install-recommends iptables \
    && rm -rf /var/lib/apt/lists/*

# ── 运行时/编译环境（两轮复盘实测缺的通用能力，非题目特化）──────────────
#   default-jre-headless : 跑 ysoserial / jar / JDWP 客户端宿主（只装 JRE 不装 JDK，省 ~350M）
#   python2              : telnetlib 等被 3.13 移除的标准库 + exploitdb 里 py2 老 PoC
#   openssh-client       : 修 "sshpass 装了但 ssh 客户端没有" 的实测 bug
#   inetutils-telnet     : 标准 telnet 客户端（自动 IAC 选项协商，别再裸 socket 手写）
#   php-cli / phpggc 类 : PHP 序列化、phar 载荷、php://filter 链需本地跑 PHP
#   make / gcc           : 编译 PoC、本地复现实测（曾出现 gcc: command not found）
#   nodejs               : JS 沙箱/前端类 PoC 需真跑 JS
#   golang-go            : Go 二进制逆向与工具（GoReSym/gore 类）
# 独立层放最后：不打爆前面各层（seclists 裁剪/pip/venv）的构建缓存。
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-jre-headless \
        python2 \
        openssh-client inetutils-telnet \
        php-cli \
        make gcc \
        nodejs \
        golang-go \
    && rm -rf /var/lib/apt/lists/*

# ── Metasploit Framework（按要求装；独立层：体积大，避免拖累其他层缓存）────
# 注意：Kali 的 metasploit-framework **硬依赖 mingw-w64 交叉工具链**（Windows payload 生成），
# 实测 --no-install-recommends 仍解出 26 包 / 约 1.7GB 原始体积（压缩后约 +380MB）。
# 体积口径见文件头：判超标看压缩后，别看 `docker images` 的原始数字。
# msf 无数据库也能用（只 loot/services 等 DB 相关功能受限）——镜像不装 postgresql-server。
RUN apt-get update && apt-get install -y --no-install-recommends \
        metasploit-framework \
    && rm -rf /var/lib/apt/lists/*

# ── 最终阶段：只拷贝成品文件系统（builder 中删除的文件不带进来）────────
FROM kalilinux/kali-rolling

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

COPY --from=builder / /

# agent 运行时 = 纯 Node（零第三方依赖的 JS 产物，见 tsec_agent/ 的 pnpm build）；
# 不再需要 Python venv（原 openai SDK 由原生 fetch 替代）。
WORKDIR /agent

# /work：hosted 托管形态没有 run.sh 挂载，记录目录与 openvpn 日志必须在镜像内预建
RUN mkdir -p /work

# 入口：容器内连 VPN（只作用于容器网络命名空间，不碰宿主机网络）→
# 等待预检通过 → 处理内网 DNS → 执行 agent
# 构建上下文 = **项目根**（在 docker/ 下执行 `docker build ..`，凭证/记录由根 .dockerignore 排除）
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# ── 离线安全知识库（196MB：CVE POC 文章 + nuclei 模板 + hacktricks 等 16 源）──
#   放最后：更新知识库只重建这一层（venv/工具层缓存不受影响）。
#   agent 用传统 Linux 搜索查它，检索路径与"别整库 grep"的规则写在 system prompt。
COPY security-knowledge-base /opt/security-knowledge-base

# ── agent 代码（hosted 交付镜像必须自带：官方平台没有 run.sh 去挂载 /agent）──
#   本地开发不受影响：run.sh 仍 `-v tsec_agent:/agent` 覆盖此层，改代码即时生效。
#   构建前必须在 tsec_agent/ 下 `pnpm install && pnpm build`（产出 dist/agent.js、
#   dist/panel-server.js、dist/panel/）——镜像内只有纯 JS 产物，不带 node_modules。
COPY tsec_agent /agent
RUN rm -f /agent/agent.log /agent/trace.* \
    && rm -rf /agent/tasks /agent/node_modules \
        /agent/src /agent/test /agent/__pycache__ 2>/dev/null || true \
    && rm -f /agent/tsconfig.json /agent/vite.config.ts /agent/vite.node.config.ts \
        /agent/vitest.config.ts /agent/pnpm-workspace.yaml /agent/index.html \
        /agent/biome.json /agent/biome.jsonc /agent/pnpm-lock.yaml \
        /agent/package.json 2>/dev/null || true
RUN node --check /agent/dist/agent.js && node --check /agent/dist/panel-server.js

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
