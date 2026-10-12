# 镜像大小硬约束：≤ 3GB（构建后 `docker images tsec-agent` 验证）。
# 选包原则：按领域覆盖、同类去重（每类留代表工具）；题目特化资源一律不进镜像。
#
# 两阶段构建：阶段1 全量安装+裁剪（裁掉的文件留在中间层）；阶段2 只 COPY 最终
# 文件系统——否则 seclists 这种"装 1.9G 再删"会把镜像层撑爆。
# 已知连带依赖教训（勿再引入）：
#   commix → unicorn-magic → metasploit(632M) + oracle-instantclient(206M) + mingw(490M)
#   hashcat → pocl-opencl-icd(127M)（容器无 GPU，hashcat 无意义，用 john 代替）

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

# ── 最终阶段：只拷贝成品文件系统（builder 中删除的文件不带进来）────────
FROM kalilinux/kali-rolling

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

COPY --from=builder / /

# agent 运行依赖（独立 venv，避免系统 python externally-managed）
RUN python3 -m venv /opt/agent-venv \
    && /opt/agent-venv/bin/pip install --no-cache-dir openai requests

WORKDIR /agent

# 入口：容器内连 VPN（只作用于容器网络命名空间，不碰宿主机网络）→
# 等待预检通过 → 处理内网 DNS → 执行 agent
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
