#!/bin/bash
# 容器入口：连接 VPN（仅容器网络命名空间内），等待联通，处理内网 DNS，然后执行命令。
set -u

VPN_CONF="${VPN_CONF:-/config/vpn.ovpn}"
VPN_LOG="${VPN_LOG:-/work/openvpn.log}"
VPN_HEALTH_URL="${VPN_HEALTH_URL:-http://10.0.100.58}"
VPN_WAIT_SEC="${VPN_WAIT_SEC:-90}"
GATEWAY_HOST="${GATEWAY_HOST:-api.deepseek.com.tsecbench.gw}"

log() { echo "[entrypoint $(date +%H:%M:%S)] $*"; }

# --- 1. 启动 openvpn ---
if pgrep -x openvpn >/dev/null; then
    log "openvpn already running"
elif [ -f "$VPN_CONF" ]; then
    log "starting openvpn with $VPN_CONF"
    openvpn --config "$VPN_CONF" --daemon --log "$VPN_LOG" --verb 3
    sleep 2
    pgrep -x openvpn >/dev/null && log "openvpn process up" || { log "openvpn failed to start"; tail -20 "$VPN_LOG"; }
else
    log "WARNING: $VPN_CONF not found, skip VPN (expect precheck to fail)"
fi

# --- 2. 等待 VPN 联通 ---
ok=0
for i in $(seq 1 $((VPN_WAIT_SEC / 3))); do
    resp=$(curl -s -m 5 "$VPN_HEALTH_URL" || true)
    if echo "$resp" | grep -q '"status"'; then
        ok=1
        log "VPN precheck OK: $resp"
        break
    fi
    sleep 3
done
if [ "$ok" != "1" ]; then
    log "FATAL: VPN precheck FAILED after ${VPN_WAIT_SEC}s — aborting (VPN检测未通过,请检查靶场VPN网络配置)"
    [ -f "$VPN_LOG" ] && tail -20 "$VPN_LOG"
    exit 11
fi

# --- 3. 内网 DNS：优先用 VPN push 的 DNS 解析网关域名 ---
# openvpn 的 dns-updown 可能已写入 resolv.conf；兜底从日志提取（格式：
# "dhcp-option DNS 10.0.100.135" 出现在 PUSH_REPLY 中，或 "DNS1: x.x.x.x"）
if ! getent hosts "$GATEWAY_HOST" >/dev/null 2>&1; then
    dns_ip=$(grep -oE 'dhcp-option DNS ([0-9]{1,3}\.){3}[0-9]{1,3}' "$VPN_LOG" 2>/dev/null | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -1)
    [ -n "${dns_ip:-}" ] || dns_ip=$(grep -oE 'DNS1:? ([0-9]{1,3}\.){3}[0-9]{1,3}' "$VPN_LOG" 2>/dev/null | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | head -1)
    if [ -n "${dns_ip:-}" ]; then
        log "using VPN-pushed DNS $dns_ip for $GATEWAY_HOST"
        echo "nameserver $dns_ip" > /etc/resolv.conf
    else
        log "no VPN-pushed DNS found in log"
    fi
    if getent hosts "$GATEWAY_HOST" >/dev/null 2>&1; then
        log "gateway DNS resolved: $(getent hosts $GATEWAY_HOST | head -1)"
    else
        log "WARNING: cannot resolve $GATEWAY_HOST"
    fi
fi

# --- 4. 出站网络限制（限网模式）---
# NET_RESTRICT=1：出站白名单——lo 全通、tun0(VPN隧道) 全通、VPN服务器:端口(保重连)、
# 平台API域名IP；其余出站全部 DROP。只作用于容器 netns（docker 授予 NET_ADMIN），不碰宿主机。
# NET_RESTRICT=auto（默认）：探测公网 AI API——可达=local 开发模式(不禁网)，
# 不可达=hosted 托管模式(禁网)。NET_RESTRICT=0 显式禁用。
restrict_network() {
    local vpn_srv vpn_port bench_host
    vpn_srv=$(awk '/^remote /{print $2; exit}' "$VPN_CONF" 2>/dev/null)
    vpn_port=$(awk '/^remote /{print $3; exit}' "$VPN_CONF" 2>/dev/null)
    vpn_port="${vpn_port:-1194}"
    bench_host=$(printf '%s' "${BENCHMARK_BASE_URL:-}" | sed -E 's|^[a-z]+://([^/:]+).*|\1|')

    iptables -N TSEC_OUT 2>/dev/null || iptables -F TSEC_OUT
    iptables -A TSEC_OUT -o lo -j ACCEPT
    iptables -A TSEC_OUT -o tun0 -j ACCEPT                      # 靶场内网 + 内网转写网关
    if [ -n "${vpn_srv:-}" ]; then
        iptables -A TSEC_OUT -o eth0 -d "$vpn_srv" -p udp --dport "$vpn_port" -j ACCEPT
        iptables -A TSEC_OUT -o eth0 -d "$vpn_srv" -p tcp --dport "$vpn_port" -j ACCEPT
    fi
    if [ -n "${bench_host:-}" ] && [ "$bench_host" != "${BENCHMARK_BASE_URL:-}" ]; then
        local ip
        for ip in $(getent ahosts "$bench_host" 2>/dev/null | awk '{print $1}' | sort -u); do
            iptables -A TSEC_OUT -o eth0 -d "$ip" -j ACCEPT
            log "allow bench api egress: $bench_host -> $ip"
        done
    fi
    iptables -A TSEC_OUT -o eth0 -j DROP
    iptables -C OUTPUT -j TSEC_OUT 2>/dev/null || iptables -I OUTPUT 1 -j TSEC_OUT
    # IPv6 一律不出站（避免 v6 绕过）
    command -v ip6tables >/dev/null && { ip6tables -I OUTPUT 1 -o eth0 -j DROP 2>/dev/null || true; }
    log "egress restricted: whitelisted (lo, tun0, vpn-server, bench-api); others DROP"
}

NET_RESTRICT="${NET_RESTRICT:-auto}"
if [ "$NET_RESTRICT" = "auto" ]; then
    if timeout 5 curl -s -o /dev/null https://api.deepseek.com/v1/models 2>/dev/null; then
        NET_RESTRICT=0
        log "NET_RESTRICT=auto -> 0 (local mode: public AI API reachable, no egress lockdown)"
    else
        NET_RESTRICT=1
        log "NET_RESTRICT=auto -> 1 (hosted mode: AI API via VPN, public egress will be blocked)"
    fi
fi
if [ "$NET_RESTRICT" = "1" ]; then
    restrict_network
else
    log "NET_RESTRICT=0: egress NOT restricted (dev/local mode)"
fi

# --- 5. 执行传入命令（默认：先冒烟自测，通过后正式跑分） ---
if [ $# -eq 0 ]; then
    if /opt/agent-venv/bin/python3 /agent/agent.py --smoke; then
        log "smoke test passed, starting full run"
        set -- /opt/agent-venv/bin/python3 /agent/agent.py
    else
        log "FATAL: smoke test failed (model gateway / shell / VPN precheck)"
        exit 12
    fi
fi
log "exec: $*"
exec "$@"
