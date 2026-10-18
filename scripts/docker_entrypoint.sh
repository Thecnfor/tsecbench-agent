#!/bin/sh
# 本地 Docker 运行入口（不进入交付镜像）：容器内拨 VPN + 默认拒绝公网。
# VPN_MODE=vpn|none   none 用于无现成靶场时自检网络守卫
# NETGUARD=on|off     默认 on
# 其余参数作为命令执行（缺省 = 编排器）
set -eu

VPN_MODE="${VPN_MODE:-vpn}"
if [ "$VPN_MODE" = "vpn" ]; then
    if [ -z "${VPN_CONFIG:-}" ]; then
        echo "[entry] VPN_MODE=vpn but VPN_CONFIG empty" >&2
        exit 2
    fi
    mkdir -p /run/tsec
    umask 077
    printf '%s\n' "$VPN_CONFIG" > /run/tsec/vpn.ovpn
    openvpn --config /run/tsec/vpn.ovpn --daemon \
        --log /run/tsec/openvpn.log --writepid /run/tsec/openvpn.pid
    i=0
    while [ "$i" -lt 45 ]; do
        ip link show tun0 >/dev/null 2>&1 && break
        i=$((i + 1))
        sleep 1
    done
    if ! ip link show tun0 >/dev/null 2>&1; then
        echo "[entry] VPN did not come up" >&2
        tail -20 /run/tsec/openvpn.log >&2 || true
        exit 3
    fi
    echo "[entry] VPN up: $(ip -4 addr show tun0 | awk '/inet /{print $2; exit}')"
fi

url_host() { printf '%s' "$1" | sed -E 's#^[A-Za-z][A-Za-z0-9+.-]*://##; s#[/:].*$##'; }
resolve_v4() { getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' | sort -u; }

if [ "${NETGUARD:-on}" = "on" ]; then
    PLATFORM_IPS=""
    [ -n "${BENCHMARK_BASE_URL:-}" ] && PLATFORM_IPS=$(resolve_v4 "$(url_host "$BENCHMARK_BASE_URL")")
    GW_IPS=$(resolve_v4 "${LLM_GATEWAY_HOST:-api.deepseek.com.tsecbench.gw}")
    VPN_IPS=$(printf '%s\n' "${VPN_CONFIG:-}" | awk '$1=="remote"{print $2; exit}' | while read -r h; do
        [ -n "$h" ] || continue
        case "$h" in *[!0-9.]*) resolve_v4 "$h" ;; *) printf '%s\n' "$h" ;; esac
    done)

    iptables -F
    iptables -A INPUT -i lo -j ACCEPT
    iptables -A OUTPUT -o lo -j ACCEPT
    iptables -A INPUT -i tun+ -j ACCEPT
    iptables -A OUTPUT -o tun+ -j ACCEPT
    iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    for cidr in ${TARGET_CIDRS:-10.0.0.0/8 172.16.0.0/12 192.168.0.0/16}; do
        iptables -A OUTPUT -d "$cidr" -j ACCEPT
    done
    for ip in $VPN_IPS; do iptables -A OUTPUT -d "$ip" -j ACCEPT; done
    for ip in $PLATFORM_IPS; do iptables -A OUTPUT -d "$ip" -p tcp --dport 443 -j ACCEPT; done
    for ip in $GW_IPS; do
        iptables -A OUTPUT -d "$ip" -p tcp --dport "${LLM_GATEWAY_PORT:-8080}" -j ACCEPT
    done
    iptables -P INPUT DROP
    iptables -P FORWARD DROP
    iptables -P OUTPUT DROP
    if command -v ip6tables >/dev/null 2>&1; then
        ip6tables -F 2>/dev/null || true
        ip6tables -P INPUT DROP 2>/dev/null || true
        ip6tables -P OUTPUT DROP 2>/dev/null || true
        ip6tables -P FORWARD DROP 2>/dev/null || true
    fi
    echo "[entry] netguard on: only VPN/targets + platform API + LLM gateway"
fi

mkdir -p /workspaces
cd /
if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exec /opt/tsecbench/venv/bin/python -m orchestrator
