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

# --- 4. 执行传入命令（默认：先冒烟自测，通过后正式跑分） ---
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
