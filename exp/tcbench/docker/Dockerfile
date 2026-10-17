# 镜像大小硬约束：push 压缩后 ≤ 3GB。**别用 `docker images` 的数字判超标**——它显示的是
# 原始（未压缩）体积，与 3GB 口径不同（实测比值约 4~5x）。压缩实测：
#   docker save tsec-agent:latest | gzip -1 -c | wc -c
# 选包原则：按领域覆盖、同类去重（每类留代表工具）；题目特化资源一律不进镜像。
#
# 两阶段构建：阶段1 全量安装+裁剪（裁掉的文件留在中间层）；阶段2 只 COPY 最终
# 文件系统——否则 seclists 这种"装 1.9G 再删"会把镜像层撑爆。
#
# 版本口径（不搞 Nix 级锁定，够用即可）：
#   - 系统基底：两个 FROM 都按 **digest 钉死**（= 当前 kali-rolling 最新内容，2026-10-11）。
#     升级系统 = `docker pull kalilinux/kali-rolling` 后把两处 sha256 换成新 digest（同一字符串）。
#   - apt 工具：随 kali-rolling 滚动用最新（Kali 源只留当前版本，硬钉 `pkg=ver` 过几周必然构建失败）；
#     当次解析到的全部版本落在镜像内 **/opt/toolchain.lock**（dpkg-query -W + pip3 list），
#     要复现旧版本/硬钉某个包时照它写 `pkg=ver` 即可。
#   - pip：PyPI 版本长期可取，**精确钉死**（见 volatility3 那层）。
# 已知连带依赖教训（勿再"无意间"引入）：
#   commix → unicorn-magic → metasploit(632M) + oracle-instantclient(206M) + mingw(490M)
#   hashcat → pocl-opencl-icd + libpocl2t64/libllvm21（原始约 500M）：容器无 GPU，
#     hashcat 只能走 CPU（pocl），只用于**离线、对时延不敏感**的哈希爆破，别拿它跑在线破解
#   注：metasploit 与 hashcat 都是**有意安装**（见文件尾的独立层），别靠 commix 这类
#   间接依赖把它们带进来，也不要为了 msf 去装 postgresql-server（msf 无库可用，DB 功能受限即可）。
#   同理别碰：oracle-instantclient、seclists 全量（已裁剪）、nuclei-templates 全量
#   （在线模板库 GB 级，且 offline 知识库 security-knowledge-base 里已有一份精选）。

FROM kalilinux/kali-rolling@sha256:02f5d6c019e549b0f55da58e835cad5355ef1030c6b2645265a324e37dfc4acc AS builder

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
        sqlmap gobuster ffuf dirb nikto feroxbuster \
        wpscan wafw00f sslscan \
        hydra john sshpass \
        openvpn \
        curl wget ca-certificates \
        netcat-openbsd busybox-static dnsutils xxd jq git \
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
        ! -name "Web-Content" ! -name "DNS" -exec rm -rf {} + ; \
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

# ── 批量漏扫 / 内网协议 / 无线（按要求装；体积小的合一层，独立于前面缓存）──
#   nuclei            : 模板化漏扫（模板走离线知识库 /opt/security-knowledge-base，不装 nuclei-templates）
#   responder         : LLMNR/NBT-NS/MDNS 抢答与 SMB 中继
#   netexec           : 内网协议横移（硬依赖 python3-impacket 已在前面，自带 bloodhound.py 采集器）
#   bloodhound-ce-python : BloodHound CE 图数据采集器（只装采集器：neo4j 服务端硬依赖 +1GB，超预算）
#   aircrack-ng       : 无线审计
#   python3-yara/pycryptodome/artifacts : volatility3 插件与通用逆向取证的可选依赖（几 MB）
RUN apt-get update && apt-get install -y --no-install-recommends \
        nuclei responder netexec bloodhound-ce-python sharphound aircrack-ng \
        python3-yara python3-pycryptodome python3-artifacts \
    && rm -rf /var/lib/apt/lists/*

# ── OWASP ZAP（Web 漏扫；无头跑 -cmd/-quickapi）────────────────────────────
#   独立层：拉 default-jre 的 GUI 部分（headless JRE 前面已装，这里只补 GUI 库）
RUN apt-get update && apt-get install -y --no-install-recommends \
        zaproxy \
    && rm -rf /var/lib/apt/lists/*

# ── hashcat（按要求装；独立层：连带 pocl/libllvm 约 500M 原始体积）──────────
#   容器无 GPU → 只能 CPU(pocl) 爆破，适合离线哈希；在线/快爆破仍用 john/hydra。
RUN apt-get update && apt-get install -y --no-install-recommends \
        hashcat \
    && rm -rf /var/lib/apt/lists/*

# ── Ghidra（逆向；按要求装；最大单层 ~1.2G 原始体积，放最后免得拖累前面缓存）──
#   Kali 包硬依赖 openjdk-25-jdk（Ghidra 12 需 JDK21+，连带 jdk-headless ~350M）；
#   --no-install-recommends 会跳过 ghidra-data 样例工程。
#   headless 分析器 /usr/share/ghidra/support/analyzeHeadless 不在 PATH → 链到 /usr/local/bin，
#   无显示的容器里模型只能用它（ghidraRun 起 GUI 会直接失败）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ghidra \
    && ln -sf /usr/share/ghidra/support/analyzeHeadless /usr/local/bin/analyzeHeadless \
    && rm -rf /var/lib/apt/lists/*

# ── Volatility 3（内存取证；Kali 源无 volatility 包，走 pip 装 wheel）────────
#   核心依赖只有 pefile（yara/pycryptodome 已由 apt 那层提供）；--no-cache-dir 不留 pip 缓存
RUN pip3 install --break-system-packages --no-cache-dir volatility3==2.28.2 \
    || pip3 install --no-cache-dir volatility3==2.28.2

# ── python 脚本运行时 lint（tsec_agent/src/py_lint.ts 检测 ruff，缺失时 stdlib ast 兜底）──
# 放 lock 层前：实际版本随本层之后的 toolchain.lock 记录（宽松安装不编造钉死号）；
# 放尾部独立层：只新建一层，前面全部缓存命中（Dockerfile 缓存纪律同 Ghidra/独立层先例）。
RUN pip3 install --break-system-packages --no-cache-dir ruff \
    || pip3 install --no-cache-dir ruff

# ── 可搬运静态客户端（/opt/portable）：靶机缺 mysql/redis/socat 时直接 scp 上去用 ──
# 起因（b-03 复盘）：Web 容器无 mysql/redis-cli/nc，横向时被迫手写 MySQL/Redis 协议客户端。
# 做法：把 Kali 里的动态二进制 + 它依赖的全部 .so + ld-linux loader 打包成单目录 + run.sh，
#  用 loader --library-path 指向包内库执行 —— 不依赖目标机装了什么、glibc 版本也自带，
#  scp 整个目录后 ./run.sh 即跑（比真·musl 静态更好维护：不用交叉编译，直接用 apt 的产物）。
RUN set -eux; \
    mkdir -p /opt/portable; \
    bundle() { \
        bin="$1"; dir="/opt/portable/$(basename "$bin")"; mkdir -p "$dir"; \
        cp "$(command -v "$bin")" "$dir/"; \
        ldd "$(command -v "$bin")" | awk '{print $3}' | grep '^/' | sort -u \
            | while read -r lib; do cp -n "$lib" "$dir/" 2>/dev/null || true; done; \
        loader=$(ldd "$(command -v "$bin")" | awk '/ld-linux/{print $1}' | head -1); \
        [ -n "$loader" ] && cp -n "$loader" "$dir/"; \
        { echo '#!/bin/sh'; \
          echo 'D=$(cd "$(dirname "$0")" && pwd)'; \
          echo "exec \"\$D/$(basename "$loader")\" --library-path \"\$D\" \"\$D/$(basename "$bin")\" \"\$@\""; \
        } > "$dir/run.sh"; \
        chmod +x "$dir/run.sh"; \
    }; \
    bundle mysql; bundle redis-cli; bundle socat; \
    ls -la /opt/portable

# ── LFI / 403 绕过专用小工具（源码构建；apt 无包）────────────────────────────
#   kadimus   : LFI 扫描/利用（b-01 曾自写多线程 LFI oracle 代替它）
#   nomore403 : 403/40X 绕过（b-01/b-03 的面板 403 卡过）
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends \
        libcurl4-openssl-dev libpcre3-dev libssh-dev \
    ; \
    git clone --depth 1 https://github.com/P0cL4bs/Kadimus /tmp/kadimus \
    && make -C /tmp/kadimus -j"$(nproc)" \
    && cp /tmp/kadimus/kadimus /usr/local/bin/kadimus \
    && rm -rf /tmp/kadimus \
    ; \
    export GOPROXY=https://goproxy.cn,direct GOFLAGS=-mod=mod; \
    git clone --depth 1 https://github.com/devploit/nomore403 /tmp/n403 \
    && (cd /tmp/n403 && go build -o /usr/local/bin/nomore403 .) \
    && rm -rf /tmp/n403 "$(go env GOPATH)"/pkg/mod "$(go env GOCACHE)" \
    ; \
    apt-get purge -y --auto-remove libcurl4-openssl-dev libpcre3-dev libssh-dev \
    && rm -rf /var/lib/apt/lists/* \
    ; \
    kadimus --help 2>&1 | head -1; nomore403 -h 2>&1 | head -1

# ── 最终瘦身（builder 的最后一次清理，删掉的东西不进成品文件系统）──────────
# 前面每个工具层都会把自己那批 doc/man/locale 带回来（清理层在它们之前），这里统一收口：
#   doc/man/locale/lintian/bug/omf : 与前段清理层同口径，纯文档与翻译
#   jvm */jmods + */man + */demo   : 只有 jlink/jpackage 用得上（~340M，jmod=zip 不可再压），
#                                    运行期 java/编译类工具都不读它
#   ghidra/Extensions             : 可选扩展 zip（Jython/Z3/样例…），只在 GUI 扩展管理器里装，
#                                   headless analyzeHeadless 不加载
# ⚠️ 删除**必须发生在 builder 阶段**（本阶段的删除会随 `COPY --from=builder / /` 进成品）；
#   放到最终阶段只会加一层 whiteout：基底镜像自身的 doc/locale 字节仍在 save 的 tar 里，
#   实测压缩体积反而 +176KB（save|gzip 是按层收字节的，白花一层）。
# 实测合计约：原始 560M / 压缩 450M——3GB 预算主要靠这一层（见文件头的体积口径）
RUN rm -rf /usr/share/doc /usr/share/man /usr/share/locale /usr/share/lintian \
           /usr/share/bug /usr/share/omf \
           /usr/lib/jvm/*/jmods /usr/lib/jvm/*/man /usr/lib/jvm/*/demo \
           /usr/share/ghidra/Extensions \
           /var/log/* /var/cache/apt/* /var/lib/apt/lists/* \
    && true

# ── 版本锁文件：把本次构建实际解析到的版本落盘，供复现/硬钉参考 ──────────
#   apt 随 kali-rolling 滚动（源只留当前版本，硬钉 pkg=ver 会随更新失效）；
#   需要复现或钉死某个包时，从这里抄版本号写成 `pkg=ver` 即可。
RUN { \
        echo "# tsec-agent 镜像版本锁（构建自动生成；基底 digest 见 docker/Dockerfile 的 FROM 行）"; \
        echo "# 升级：改 FROM 的 sha256 / pip 钉的版本；apt 工具跟随 kali-rolling 最新"; \
        echo; echo "## dpkg"; dpkg-query -W; \
        echo; echo "## pip"; pip3 list --format=freeze 2>/dev/null; \
    } > /opt/toolchain.lock && wc -l /opt/toolchain.lock

# ── 最终阶段：只拷贝成品文件系统（builder 中删除的文件不带进来）────────
FROM kalilinux/kali-rolling@sha256:02f5d6c019e549b0f55da58e835cad5355ef1030c6b2645265a324e37dfc4acc

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

# ── 离线安全知识库（CVE POC 文章 + nuclei 模板 + hacktricks 等 16 源 + KEV/NVD 数据）──
#   放最后：更新知识库只重建这一层（venv/工具层缓存不受影响）。
COPY security-knowledge-base /opt/security-knowledge-base

# ── kb 检索引擎（索引 CLI + 改写检索子 agent；索引构建期一次性，约 10 秒）──
#   产物：<KB>/.index/（kb.sqlite 等）；检索通道与纪律写在 system prompt，用法见 docs/kb-search.md
COPY scripts/build_kb_index.py scripts/build_cve_data.py scripts/kb \
     scripts/kb-hunter.py scripts/kb-hunter.md /usr/local/bin/
RUN chmod +x /usr/local/bin/kb /usr/local/bin/kb-hunter.py \
    && python3 /usr/local/bin/build_kb_index.py --kb /opt/security-knowledge-base

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
