# 镜像大小硬约束：≤ 3GB（当前约 1.2GB）。加新工具前先估算体积，
# 构建后用 `docker images tsec-agent` 验证。
FROM kalilinux/kali-rolling

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# 固定国内 Kali 镜像：http.kali.org 会 302 到 mirrors.neusoft.edu.cn（仅 IPv6，
# 容器内解析失败）；TUNA/USTC/阿里云可达。用 http 而非 https——apt update 阶段
# ca-certificates 尚未安装，https 会证书校验失败
RUN set -eux; \
    printf 'Types: deb\nURIs: http://mirrors.tuna.tsinghua.edu.cn/kali\nSuites: kali-rolling\nComponents: main contrib non-free non-free-firmware\nSigned-By: /usr/share/keyrings/kali-archive-keyring.gpg\n' \
        > /etc/apt/sources.list.d/kali.sources; \
    printf 'deb http://mirrors.ustc.edu.cn/kali kali-rolling main contrib non-free non-free-firmware\ndeb http://mirrors.aliyun.com/kali kali-rolling main contrib non-free non-free-firmware\n' \
        > /etc/apt/sources.list.d/kali-backup.list; \
    rm -f /etc/apt/sources.list

# 渗透与网络工具链（体积可控，字典用 dirb 自带的）
RUN apt-get update && apt-get install -y --no-install-recommends \
        nmap masscan whatweb \
        sqlmap gobuster ffuf dirb nikto \
        hydra \
        openvpn \
        curl wget ca-certificates \
        netcat-openbsd dnsutils xxd jq git \
        python3 python3-pip python3-venv python3-requests \
        procps iproute2 iputils-ping traceroute tcpdump \
        seclists \
    && rm -rf /var/lib/apt/lists/* \
    && find /usr/share/seclists -mindepth 1 -maxdepth 1 \
       ! -name "Discovery" ! -name "Payloads" -exec rm -rf {} + \
    && find /usr/share/seclists/Discovery -mindepth 1 -maxdepth 1 \
       ! -name "Web-Content" -exec rm -rf {} + \
    && find /usr/share/seclists -type f -size +1M -delete

# agent 运行依赖（kali pip externally-managed → 用 venv）
RUN python3 -m venv /opt/agent-venv \
    && /opt/agent-venv/bin/pip install --no-cache-dir openai requests

WORKDIR /agent

# 入口：容器内连 VPN（只作用于容器网络命名空间，不碰宿主机网络）→
# 等待预检通过 → 处理内网 DNS → 执行 agent
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
