#!/bin/bash
# 容器入口：连接 VPN（仅容器网络命名空间内），等待联通，处理内网 DNS，然后执行命令。
set -u

VPN_CONF="${VPN_CONF:-/config/vpn.ovpn}"
VPN_LOG="${VPN_LOG:-/work/openvpn.log}"
VPN_HEALTH_URL="${VPN_HEALTH_URL:-http://10.0.100.58}"
VPN_WAIT_SEC="${VPN_WAIT_SEC:-90}"

log() { echo "[entrypoint $(date +%H:%M:%S)] $*"; }

# --- 0. hosted 交付准备（官方平台只注入环境变量：无 run.sh、无卷挂载）---
# /work 不存在则自建：openvpn 日志与 agent 记录（records.py）都落这里
mkdir -p /work "$(dirname "$VPN_LOG")" 2>/dev/null || true
# 托管模式可用 VPN_CONFIG 环境变量下发完整 .ovpn 内容（文件挂载的替代通道，umask 0700 落盘）
if [ ! -f "$VPN_CONF" ] && [ -n "${VPN_CONFIG:-}" ]; then
    if ! mkdir -p "$(dirname "$VPN_CONF")" 2>/dev/null; then
        VPN_CONF=/tmp/vpn.ovpn
    fi
    log "VPN_CONFIG env provided; materialized $VPN_CONF"
    (umask 077; printf '%s\n' "$VPN_CONFIG" > "$VPN_CONF")
fi
# 模型网关域名：显式 GATEWAY_HOST > 从 DEEPSEEK_BASE_URL(_HOSTED) 按官方转写规则推导
# > 内置默认。取 host 语义与 config.py: to_gateway_url 一致：去 scheme、去 path、去 port。
_gw_src="${DEEPSEEK_BASE_URL_HOSTED:-${DEEPSEEK_BASE_URL:-}}"
_gw_host="${_gw_src#*://}"; _gw_host="${_gw_host%%/*}"; _gw_host="${_gw_host%%:*}"
if [ -z "${GATEWAY_HOST:-}" ]; then
    if [ -n "$_gw_host" ]; then
        case "$_gw_host" in
            *.tsecbench.gw) GATEWAY_HOST="$_gw_host" ;;
            *)              GATEWAY_HOST="$_gw_host.tsecbench.gw" ;;
        esac
    else
        GATEWAY_HOST="api.deepseek.com.tsecbench.gw"
    fi
fi
log "model gateway host: $GATEWAY_HOST"

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
# 平台API域名IP、模型转写网关域名IP；其余出站全部 DROP。只作用于容器 netns（docker 授予
# NET_ADMIN），不碰宿主机。
# NET_RESTRICT=auto（默认）：探测公网 AI API——可达=local 开发模式(不禁网)，
# 不可达=hosted 托管模式(禁网)。NET_RESTRICT=0 显式禁用。
# 失败安全：平台 API 或模型网关域名解析不出 IP 时**不锁网**（fail-open）——白名单缺了
# 关键域名还要 DROP 全部出站，等于自己制造"容器起得来但题库/模型全不通"，比不限网更糟。
restrict_network() {
    local vpn_srv vpn_port bench_host gw_host resolved
    # 能力预检：官方托管可能不授予 NET_ADMIN（iptables 报 Permission denied）。
    # 此时容器内根本锁不了网——必须明确告警并跳过，绝不能在一条规则都没落下的情况下
    # 打出 "egress restricted" 假成功日志（曾误导排障）。出站限制由平台网络层承担。
    if ! iptables -t filter -L -n >/dev/null 2>&1; then
        log "WARNING: NET_ADMIN not granted by platform (iptables permission denied) — skipping egress lockdown; 出站限制交由平台网络层"
        return 0
    fi
    vpn_srv=$(awk '/^remote /{print $2; exit}' "$VPN_CONF" 2>/dev/null)
    vpn_port=$(awk '/^remote /{print $3; exit}' "$VPN_CONF" 2>/dev/null)
    vpn_port="${vpn_port:-1194}"
    # 平台 API 域名：BENCHMARK_BASE_URL 可能带/不带 scheme、带端口/路径，只取 host
    bench_host=$(printf '%s' "${BENCHMARK_BASE_URL:-}" | sed -E 's|^[a-z]+://||; s|[:/].*||')
    gw_host="$GATEWAY_HOST"

    # 解析预检：白名单的两个关键域名必须先能解析出 IP，否则锁网必然误伤
    resolved=1
    if [ -n "${bench_host:-}" ] && ! getent ahosts "$bench_host" >/dev/null 2>&1; then
        log "FATAL-SAFE: bench api host '$bench_host' does NOT resolve — egress will NOT be restricted"
        resolved=0
    fi
    if ! getent ahosts "$gw_host" >/dev/null 2>&1; then
        log "FATAL-SAFE: model gateway host '$gw_host' does NOT resolve — egress will NOT be restricted"
        resolved=0
    fi
    if [ "$resolved" != "1" ]; then
        log "skip lockdown (unresolvable whitelist entries would blackhole bench/gateway traffic)"
        return 0
    fi

    iptables -N TSEC_OUT 2>/dev/null || iptables -F TSEC_OUT
    iptables -A TSEC_OUT -o lo -j ACCEPT
    iptables -A TSEC_OUT -o tun0 -j ACCEPT                      # 靶场内网 + 内网转写网关
    # DNS 必须放行：resolv.conf 里的 nameserver 走 eth0，锁死它 = 全容器解析瘫痪，
    # 白名单域名在 curl 运行时重新解析时全部失败（实测： lockdown 后 getent/curl 全挂）
    local ns
    for ns in $(awk '/^nameserver /{print $2}' /etc/resolv.conf 2>/dev/null); do
        iptables -A TSEC_OUT -o eth0 -d "$ns" -p udp --dport 53 -j ACCEPT
        iptables -A TSEC_OUT -o eth0 -d "$ns" -p tcp --dport 53 -j ACCEPT
        log "allow DNS egress: $ns"
    done
    if [ -n "${vpn_srv:-}" ]; then
        iptables -A TSEC_OUT -o eth0 -d "$vpn_srv" -p udp --dport "$vpn_port" -j ACCEPT
        iptables -A TSEC_OUT -o eth0 -d "$vpn_srv" -p tcp --dport "$vpn_port" -j ACCEPT
    fi
    local ip
    if [ -n "${bench_host:-}" ]; then
        for ip in $(getent ahosts "$bench_host" 2>/dev/null | awk '{print $1}' | sort -u); do
            iptables -A TSEC_OUT -o eth0 -d "$ip" -j ACCEPT
            log "allow bench api egress: $bench_host -> $ip"
        done
    fi
    # 模型网关：hosted 里它经 eth0 直连（转写网关入口）或经 tun0（VPN 内网），
    # 两个口子都放行——只放 tun0 时平台网络形态一变，全部 LLM 调用被 DROP、冒烟必挂
    for ip in $(getent ahosts "$gw_host" 2>/dev/null | awk '{print $1}' | sort -u); do
        iptables -A TSEC_OUT -o eth0 -d "$ip" -j ACCEPT
        log "allow model gateway egress: $gw_host -> $ip"
    done
    iptables -A TSEC_OUT -o eth0 -j DROP
    iptables -C OUTPUT -j TSEC_OUT 2>/dev/null || iptables -I OUTPUT 1 -j TSEC_OUT
    # IPv6 一律不出站（避免 v6 绕过）
    command -v ip6tables >/dev/null && { ip6tables -I OUTPUT 1 -o eth0 -j DROP 2>/dev/null || true; }
    log "egress restricted: whitelisted (lo, tun0, vpn-server, bench-api, model-gateway); others DROP"
}

NET_RESTRICT="${NET_RESTRICT:-auto}"
if [ "$NET_RESTRICT" = "auto" ]; then
    if timeout 5 curl -s -o /dev/null https://api.deepseek.com/v1/models 2>/dev/null; then
        NET_RESTRICT=0
        log "NET_RESTRICT=auto -> 0 (local mode: public AI API reachable, no egress lockdown)"
    else
        NET_RESTRICT=1
        log "NET_RESTRICT=auto -> 1 (hosted mode: AI API via VPN, public egress will be blocked)"
        # 公网 AI API 不可达 ⟹ local 后端必然跑不通：未显式指定模式时直接导出 hosted，
        # 免得 agent 侧 auto 兜底回 local（实测：平台侧设了 DEEPSEEK_API_KEY 后，
        # hosted 探测一旦抖动 → 有 key 就判 local → 打被平台封禁的公网 API →
        # 冒烟 APIConnectionError exit 12）。显式 AGENT_MODE=local|hosted 时尊重用户选择。
        case "${AGENT_MODE:-auto}" in
            auto|"")
                export AGENT_MODE=hosted
                log "AGENT_MODE: auto -> hosted (public AI API unreachable; local backend cannot work here)"
                ;;
        esac
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
